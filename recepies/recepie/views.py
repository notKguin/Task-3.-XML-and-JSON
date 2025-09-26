from __future__ import annotations
import json
import uuid
import shutil
from pathlib import Path
from typing import Any, Dict, List

from django.conf import settings
from django import forms
from django.http import HttpResponse, Http404, HttpRequest
from django.shortcuts import redirect
from django.contrib import messages
from django.utils.html import escape
from django.views.decorators.http import require_http_methods
from django.template import engines

# ---------- Простые формы (всё в одном файле, без дополнительных файлов) ----------

class IngredientField(forms.CharField):
    """
    Поле для ввода ингредиентов: один ингредиент в строке
    формат: Название;количество;единица
    пример: Сахар;50;г
    """
    def to_python(self, value):
        value = super().to_python(value)
        # Разрешаем пустое — потом проверим на уровне всей формы
        return value

class RecipeForm(forms.Form):
    """
    Форма ввода рецепта. Валидируем структуру.
    """
    title = forms.CharField(label="Название рецепта", max_length=100)
    servings = forms.IntegerField(label="Порций", min_value=1)
    # Многострочный ввод ингредиентов и шагов — проще всего
    ingredients = forms.CharField(
        label="Ингредиенты (по одному на строку: Название;количество;ед.)",
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    steps = forms.CharField(
        label="Шаги приготовления (каждый шаг — с новой строки)",
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    format = forms.ChoiceField(
        label="Сохранить как",
        choices=(("json", "JSON"), ("xml", "XML")),
    )

    def clean(self):
        cleaned = super().clean()

        # Разбор ингредиентов
        ing_raw = cleaned.get("ingredients", "").strip()
        if not ing_raw:
            raise forms.ValidationError("Укажите хотя бы один ингредиент.")
        ingredients: List[Dict[str, Any]] = []
        for i, line in enumerate(ing_raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) != 3:
                raise forms.ValidationError(f"Ингредиент #{i}: используйте формат 'Название;количество;ед.'")
            name, amount, unit = parts
            if not name:
                raise forms.ValidationError(f"Ингредиент #{i}: пустое название.")
            # Количество — число (можно float)
            try:
                amount_val = float(amount.replace(",", "."))  # поддержим 1,5
            except ValueError:
                raise forms.ValidationError(f"Ингредиент #{i}: количество должно быть числом.")
            if amount_val <= 0:
                raise forms.ValidationError(f"Ингредиент #{i}: количество должно быть > 0.")
            if not unit:
                raise forms.ValidationError(f"Ингредиент #{i}: пустая единица измерения.")
            ingredients.append({"name": name, "amount": amount_val, "unit": unit})

        if not ingredients:
            raise forms.ValidationError("Добавьте хотя бы один валидный ингредиент.")
        cleaned["ingredients_parsed"] = ingredients

        # Разбор шагов
        steps_raw = cleaned.get("steps", "").strip()
        steps_list = [s.strip() for s in steps_raw.splitlines() if s.strip()]
        if not steps_list:
            raise forms.ValidationError("Добавьте хотя бы один шаг приготовления.")
        cleaned["steps_parsed"] = steps_list
        return cleaned


class UploadForm(forms.Form):
    """
    Форма загрузки JSON/XML. Имя файла пользователя игнорируем — генерим сами.
    """
    file = forms.FileField(label="Загрузить JSON/XML")


# ---------- Утилиты ----------

def _data_dir(fmt: str) -> Path:
    """
    Папка для формата json/xml: <BASE_DIR>/data/<fmt>
    """
    d = Path(getattr(settings, "DATA_DIR", settings.BASE_DIR / "data")) / fmt
    d.mkdir(parents=True, exist_ok=True)
    return d

def _safe_name(fmt: str) -> str:
    """
    Генерим безопасное имя: recipe-<uuid>.<fmt>
    """
    suffix = "json" if fmt == "json" else "xml"
    return f"recipe-{uuid.uuid4().hex}.{suffix}"

def _validate_recipe_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Универсальная валидация словаря рецепта (после парсинга JSON/XML).
    Требуем структуру:
    {
      "title": str (1..100),
      "servings": int >= 1,
      "ingredients": [ {"name": str, "amount": number>0, "unit": str}, ... ],
      "steps": [ str, ... ]
    }
    """
    if not isinstance(data, dict):
        raise ValueError("Ожидался объект рецепта.")
    title = data.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 100):
        raise ValueError("Поле 'title' должно быть строкой длиной 1..100.")
    servings = data.get("servings")
    if not isinstance(servings, (int, float)) or int(servings) < 1:
        raise ValueError("Поле 'servings' должно быть целым числом >= 1.")
    ingredients = data.get("ingredients")
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("Поле 'ingredients' должно быть непустым списком.")
    for i, ing in enumerate(ingredients, start=1):
        if not isinstance(ing, dict):
            raise ValueError(f"Ингредиент #{i}: ожидается объект.")
        if not isinstance(ing.get("name"), str) or not ing["name"]:
            raise ValueError(f"Ингредиент #{i}: поле 'name' обязательно.")
        amount = ing.get("amount")
        try:
            amount_val = float(amount)
        except Exception:
            raise ValueError(f"Ингредиент #{i}: поле 'amount' должно быть числом.")
        if amount_val <= 0:
            raise ValueError(f"Ингредиент #{i}: 'amount' должен быть > 0.")
        if not isinstance(ing.get("unit"), str) or not ing["unit"]:
            raise ValueError(f"Ингредиент #{i}: поле 'unit' обязательно.")
    steps = data.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, str) and s.strip() for s in steps):
        raise ValueError("Поле 'steps' должно быть списком непустых строк.")
    # нормализуем servings к int (если вдруг был float из XML)
    data["servings"] = int(servings)
    return data


# ---------- JSON/XML сериализация ----------

def _to_json(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")

def _from_json(raw: bytes) -> Dict[str, Any]:
    return json.loads(raw.decode("utf-8"))

def _to_xml(obj: Dict[str, Any]) -> bytes:
    # Формат:
    # <recipe>
    #   <title>...</title>
    #   <servings>2</servings>
    #   <ingredients><item name="..." unit="...">amount</item>...</ingredients>
    #   <steps><step>...</step>...</steps>
    # </recipe>
    from xml.etree.ElementTree import Element, SubElement, tostring
    r = Element("recipe")
    t = SubElement(r, "title"); t.text = obj["title"]
    s = SubElement(r, "servings"); s.text = str(obj["servings"])
    ing = SubElement(r, "ingredients")
    for item in obj["ingredients"]:
        it = SubElement(ing, "item", attrib={"name": item["name"], "unit": item["unit"]})
        it.text = str(item["amount"])
    steps = SubElement(r, "steps")
    for st in obj["steps"]:
        stn = SubElement(steps, "step"); stn.text = st
    xml_bytes = tostring(r, encoding="utf-8", xml_declaration=True)
    return xml_bytes

def _from_xml(raw: bytes) -> Dict[str, Any]:
    from xml.etree.ElementTree import fromstring
    root = fromstring(raw)
    if root.tag != "recipe":
        raise ValueError("Корневой тег должен быть <recipe>.")
    title = (root.findtext("title") or "").strip()
    servings_text = (root.findtext("servings") or "").strip()
    try:
        servings = int(float(servings_text))
    except Exception:
        raise ValueError("servings в XML должен быть числом.")
    ingredients_el = root.find("ingredients")
    if ingredients_el is None:
        raise ValueError("<ingredients> отсутствует.")
    ingredients: List[Dict[str, Any]] = []
    for i, item in enumerate(list(ingredients_el), start=1):
        if item.tag != "item":
            continue
        name = (item.attrib.get("name") or "").strip()
        unit = (item.attrib.get("unit") or "").strip()
        amount_text = (item.text or "").strip()
        try:
            amount = float(amount_text.replace(",", "."))
        except Exception:
            raise ValueError(f"Ингредиент #{i}: amount должен быть числом.")
        ingredients.append({"name": name, "unit": unit, "amount": amount})
    steps_el = root.find("steps")
    if steps_el is None:
        raise ValueError("<steps> отсутствует.")
    steps = []
    for st in list(steps_el):
        if st.tag == "step":
            val = (st.text or "").strip()
            if val:
                steps.append(val)
    return {
        "title": title,
        "servings": servings,
        "ingredients": ingredients,
        "steps": steps,
    }


# ---------- Страница (одна) ----------

INDEX_TMPL = engines["django"].from_string("""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <title>Рецепты: JSON/XML</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; }
    h1 { margin-bottom: 8px; }
    form { margin: 16px 0; padding: 12px; border: 1px solid #ddd; border-radius: 8px; }
    .row { display: grid; grid-template-columns: 160px 1fr; gap: 12px; align-items: start; margin-bottom: 10px; }
    textarea, input[type="text"], input[type="number"], select { width: 100%; padding: 6px 8px; }
    .actions { display: flex; gap: 8px; }
    .msg { background: #f6f8ff; border: 1px solid #cfd6ff; color: #223; padding: 10px; border-radius: 6px; margin: 10px 0;}
    .error { background: #fff1f1; border-color: #ffcccc; }
    .files { margin: 16px 0; }
    code { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .small { color: #666; font-size: 12px; }
  </style>
</head>
<body>
  <h1>Импорт/экспорт рецептов (JSON/XML)</h1>

  {% if messages %}
    {% for m in messages %}
      <div class="msg{% if m.tags %} {{ m.tags }}{% endif %}">{{ m }}</div>
    {% endfor %}
  {% endif %}

  <h2>1) Ввод рецепта и сохранение в файл</h2>
  <form method="post" action="{% url 'save_data' %}">
    {% csrf_token %}
    <div class="row">
      <label>Название:</label>
      {{ recipe_form.title }}
    </div>
    <div class="row">
      <label>Порций:</label>
      {{ recipe_form.servings }}
    </div>
    <div class="row">
      <label>Ингредиенты:</label>
      {{ recipe_form.ingredients }}
    </div>
    <div class="row">
      <label>Шаги:</label>
      {{ recipe_form.steps }}
    </div>
    <div class="row">
      <label>Формат:</label>
      {{ recipe_form.format }}
    </div>
    {% if recipe_form.errors %}
      <div class="msg error">
        <strong>Ошибки формы:</strong>
        <ul>
          {% for field, errs in recipe_form.errors.items %}
            {% for e in errs %}
              <li><span class="mono">{{ field }}</span>: {{ e }}</li>
            {% endfor %}
          {% endfor %}
        </ul>
      </div>
    {% endif %}
    <div class="actions">
      <button type="submit">Сохранить файл</button>
    </div>
    <div class="small">
      Пример ингредиента: <code>Сахар;50;г</code> (каждый с новой строки).
      Шаги — по одной строке.
    </div>
  </form>

  <h2>2) Загрузка готового JSON/XML и валидация</h2>
  <form method="post" enctype="multipart/form-data" action="{% url 'upload_file' %}">
    {% csrf_token %}
    {{ upload_form.file }}
    <div class="actions">
      <button type="submit">Загрузить</button>
    </div>
  </form>

  <h2>3) Файлы на сервере</h2>
  <div class="files">
    {% if not files_json and not files_xml %}
      <div class="msg">Файлы не найдены. Сохраните или загрузите что-нибудь.</div>
    {% else %}
      {% if files_json %}
        <h3>JSON ({{ files_json|length }})</h3>
        <ul>
          {% for f in files_json %}
            <li><a href="{% url 'view_file' 'json' f %}">{{ f }}</a></li>
          {% endfor %}
        </ul>
      {% endif %}
      {% if files_xml %}
        <h3>XML ({{ files_xml|length }})</h3>
        <ul>
          {% for f in files_xml %}
            <li><a href="{% url 'view_file' 'xml' f %}">{{ f }}</a></li>
          {% endfor %}
        </ul>
      {% endif %}
    {% endif %}
  </div>

  <hr/>
  <p class="small">Файлы лежат в папке: <code>{{ data_dir }}</code></p>
</body>
</html>
""")

def _list_files(fmt: str) -> List[str]:
    d = _data_dir(fmt)
    suffix = ".json" if fmt == "json" else ".xml"
    return sorted([p.name for p in d.glob(f"*{suffix}") if p.is_file()])

def _read_file(fmt: str, fname: str) -> bytes:
    # никому не доверяем: разрешаем только имена вида recipe-<uuid>.<ext>
    suffix = ".json" if fmt == "json" else ".xml"
    if not fname.startswith("recipe-") or not fname.endswith(suffix):
        raise Http404("Некорректное имя файла.")
    p = _data_dir(fmt) / fname
    if not p.exists() or not p.is_file():
        raise Http404("Файл не найден.")
    return p.read_bytes()

@require_http_methods(["GET"])
def index(request: HttpRequest) -> HttpResponse:
    recipe_form = RecipeForm()
    upload_form = UploadForm()
    ctx = {
        "recipe_form": recipe_form,
        "upload_form": upload_form,
        "files_json": _list_files("json"),
        "files_xml": _list_files("xml"),
        "data_dir": str(getattr(settings, "DATA_DIR", settings.BASE_DIR / "data")),
    }
    return HttpResponse(INDEX_TMPL.render(ctx, request))

@require_http_methods(["POST"])
def save_data(request: HttpRequest) -> HttpResponse:
    form = RecipeForm(request.POST)
    if not form.is_valid():
        # Показать те же списки файлов
        ctx = {
            "recipe_form": form,
            "upload_form": UploadForm(),
            "files_json": _list_files("json"),
            "files_xml": _list_files("xml"),
            "data_dir": str(getattr(settings, "DATA_DIR", settings.BASE_DIR / "data")),
        }
        return HttpResponse(INDEX_TMPL.render(ctx, request))

    data = {
        "title": form.cleaned_data["title"].strip(),
        "servings": form.cleaned_data["servings"],
        "ingredients": form.cleaned_data["ingredients_parsed"],
        "steps": form.cleaned_data["steps_parsed"],
    }
    fmt = form.cleaned_data["format"]
    name = _safe_name(fmt)
    path = _data_dir(fmt) / name

    if fmt == "json":
        raw = _to_json(_validate_recipe_dict(data))
    else:
        raw = _to_xml(_validate_recipe_dict(data))

    path.write_bytes(raw)
    messages.success(request, f"Файл сохранён: {escape(name)}")
    return redirect("index")

@require_http_methods(["POST"])
def upload_file(request: HttpRequest) -> HttpResponse:
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Не выбран файл.")
        return redirect("index")
    up = form.cleaned_data["file"]
    content_type = (up.content_type or "").lower()
    # Грубая проверка типа + анализ содержимого
    if content_type.endswith("/json") or up.name.lower().endswith(".json"):
        fmt = "json"
    elif content_type in ("application/xml", "text/xml") or up.name.lower().endswith(".xml"):
        fmt = "xml"
    else:
        messages.error(request, "Поддерживаются только JSON или XML.")
        return redirect("index")

    # Сохраним во временный файл в целевой папке с безопасным именем
    safe_name = _safe_name(fmt)
    target = _data_dir(fmt) / safe_name

    # Скопируем в файл
    with target.open("wb") as dst:
        for chunk in up.chunks():
            dst.write(chunk)

    # Пробуем прочитать и провалидировать; если не пройдёт — удалим
    try:
        raw = target.read_bytes()
        if fmt == "json":
            data = _from_json(raw)
        else:
            data = _from_xml(raw)
        _validate_recipe_dict(data)
    except Exception as e:
        try:
            target.unlink(missing_ok=True)
        finally:
            messages.error(request, f"Файл отклонён: {escape(str(e))}. Файл удалён.")
            return redirect("index")

    messages.success(request, f"Файл загружен и валиден: {escape(safe_name)}")
    return redirect("index")

@require_http_methods(["GET"])
def view_file(request: HttpRequest, fmt: str, fname: str) -> HttpResponse:
    fmt = (fmt or "").lower()
    if fmt not in ("json", "xml"):
        raise Http404("Неверный формат.")

    raw = _read_file(fmt, fname)

    # Отобразим красивенько на странице, + распарсим и выведем таблицей
    try:
        data = _from_json(raw) if fmt == "json" else _from_xml(raw)
        data = _validate_recipe_dict(data)
    except Exception as e:
        # Покажем просто как текст
        pre = escape(raw.decode("utf-8", errors="replace"))
        return HttpResponse(f"<pre>{pre}</pre>")

    # Сформируем HTML быстренько
    html = [
        "<!doctype html><meta charset='utf-8'><title>Просмотр файла</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:24px} table{border-collapse:collapse} td,th{border:1px solid #ddd;padding:6px 8px}</style>",
        f"<h1>{escape(fname)}</h1>",
        f"<p><a href='/'>← Назад</a></p>",
        "<h2>Содержимое</h2>",
        f"<p><strong>Название:</strong> {escape(data['title'])}</p>",
        f"<p><strong>Порций:</strong> {data['servings']}</p>",
        "<h3>Ингредиенты</h3>",
        "<table><tr><th>Название</th><th>Кол-во</th><th>Ед.</th></tr>",
    ]
    for ing in data["ingredients"]:
        html.append(
            f"<tr><td>{escape(ing['name'])}</td>"
            f"<td>{ing['amount']}</td><td>{escape(ing['unit'])}</td></tr>"
        )
    html.append("</table>")
    html.append("<h3>Шаги</h3><ol>")
    for s in data["steps"]:
        html.append(f"<li>{escape(s)}</li>")
    html.append("</ol>")
    html.append("</body></html>")
    return HttpResponse("".join(html))
