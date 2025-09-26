from __future__ import annotations
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

from django.conf import settings
from django import forms
from django.forms import formset_factory
from django.http import HttpResponse, Http404, HttpRequest
from django.shortcuts import redirect, render
from django.contrib import messages
from django.utils.html import escape
from django.views.decorators.http import require_http_methods


# ---------- Константы для единиц ----------
UNIT_CHOICES = [
    ("г", "г"),
    ("кг", "кг"),
    ("мл", "мл"),
    ("л", "л"),
    ("шт", "шт"),
    ("ч.л.", "ч.л."),
    ("ст.л.", "ст.л."),
]


# ---------- Формы ----------
class RecipeForm(forms.Form):
    title = forms.CharField(
        label="Название рецепта",
        max_length=100,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "Название рецепта"   
                                      })
    )
    servings = forms.IntegerField(
        label="Порций",
        min_value=1,
        initial=1,  # по умолчанию 1
        widget=forms.NumberInput(attrs={
            "class": "form-control form-control-sm",
            "min": "1",
            "step": "1",
        })
    )


class IngredientItemForm(forms.Form):
    name = forms.CharField(
        label="Название",
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "Сахар"
        })
    )
    amount = forms.DecimalField(
        label="Кол-во",
        min_value=0.1,
        max_digits=10,
        decimal_places=1,
        widget=forms.NumberInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "100",
            "step": "0.1",
            "min": "0",
        })
    )
    unit = forms.ChoiceField(
        label="Ед.",
        choices=UNIT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"})
    )


class StepItemForm(forms.Form):
    text = forms.CharField(
        label="Шаг",
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "Описание шага"
        })
    )


IngredientFormSet = formset_factory(IngredientItemForm, extra=1, can_delete=True)
StepFormSet = formset_factory(StepItemForm, extra=1, can_delete=True)


class UploadForm(forms.Form):
    file = forms.FileField(
        label="Загрузить JSON/XML",
        widget=forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"})
    )


# ---------- Утилиты ----------
def _data_dir(fmt: str) -> Path:
    d = Path(getattr(settings, "DATA_DIR", settings.BASE_DIR / "data")) / fmt
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(fmt: str) -> str:
    suffix = "json" if fmt == "json" else "xml"
    return f"recipe-{uuid.uuid4().hex}.{suffix}"


def _validate_recipe_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Ожидался объект рецепта.")
    title = data.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 100):
        raise ValueError("Поле 'title' должно быть строкой 1..100.")
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
            raise ValueError(f"Ингредиент #{i}: 'name' обязателен.")
        try:
            amount_val = float(ing.get("amount"))
        except Exception:
            raise ValueError(f"Ингредиент #{i}: 'amount' должен быть числом.")
        if amount_val <= 0:
            raise ValueError(f"Ингредиент #{i}: 'amount' должен быть > 0.")
        if not isinstance(ing.get("unit"), str) or not ing["unit"]:
            raise ValueError(f"Ингредиент #{i}: 'unit' обязателен.")
    steps = data.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, str) and s.strip() for s in steps):
        raise ValueError("Поле 'steps' должно быть списком непустых строк.")
    data["servings"] = int(servings)
    return data


def _to_json(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def _from_json(raw: bytes) -> Dict[str, Any]:
    return json.loads(raw.decode("utf-8"))


def _to_xml(obj: Dict[str, Any]) -> bytes:
    from xml.etree.ElementTree import Element, SubElement, tostring
    r = Element("recipe")
    SubElement(r, "title").text = obj["title"]
    SubElement(r, "servings").text = str(obj["servings"])
    ing = SubElement(r, "ingredients")
    for item in obj["ingredients"]:
        it = SubElement(ing, "item", attrib={"name": item["name"], "unit": item["unit"]})
        it.text = str(item["amount"])
    steps = SubElement(r, "steps")
    for st in obj["steps"]:
        SubElement(steps, "step").text = st
    return tostring(r, encoding="utf-8", xml_declaration=True)


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
    return {"title": title, "servings": servings, "ingredients": ingredients, "steps": steps}


def _list_files(fmt: str) -> List[str]:
    d = _data_dir(fmt)
    suffix = ".json" if fmt == "json" else ".xml"
    return sorted([p.name for p in d.glob(f"*{suffix}") if p.is_file()])


def _read_file(fmt: str, fname: str) -> bytes:
    suffix = ".json" if fmt == "json" else ".xml"
    if not fname.startswith("recipe-") or not fname.endswith(suffix):
        raise Http404("Некорректное имя файла.")
    p = _data_dir(fmt) / fname
    if not p.exists() or not p.is_file():
        raise Http404("Файл не найден.")
    return p.read_bytes()


# ---------- Вьюхи ----------
@require_http_methods(["GET"])
def index(request: HttpRequest) -> HttpResponse:
    ctx = {
        "recipe_form": RecipeForm(),
        "ing_formset": IngredientFormSet(prefix="ing"),
        "step_formset": StepFormSet(prefix="step"),
        "upload_form": UploadForm(),
        "files_json": _list_files("json"),
        "files_xml": _list_files("xml"),
        "data_dir": str(getattr(settings, "DATA_DIR", settings.BASE_DIR / "data")),
    }
    return render(request, "recepie/index.html", ctx)


@require_http_methods(["POST"])
def save_data(request: HttpRequest) -> HttpResponse:
    form = RecipeForm(request.POST)
    ing_formset = IngredientFormSet(request.POST, prefix="ing")
    step_formset = StepFormSet(request.POST, prefix="step")

    valid = form.is_valid() and ing_formset.is_valid() and step_formset.is_valid()

    # Соберём и провалидируем содержимое формсетов
    ingredients: List[Dict[str, Any]] = []
    if ing_formset.is_valid():
        for f in ing_formset:
            if f.cleaned_data.get("DELETE", False):
                continue
            name = (f.cleaned_data.get("name") or "").strip()
            amount = f.cleaned_data.get("amount")
            unit = f.cleaned_data.get("unit")
            if not name:
                f.add_error("name", "Укажите название.")
            if amount is None or float(amount) <= 0:
                f.add_error("amount", "Должно быть положительное число.")
            if not unit:
                f.add_error("unit", "Выберите единицу.")
            if name and amount and unit and float(amount) > 0:
                ingredients.append({"name": name, "amount": float(amount), "unit": unit})
        if not ingredients and ing_formset.forms:
            ing_formset.forms[0].add_error(None, "Добавьте хотя бы один ингредиент.")
            valid = False
    else:
        valid = False

    steps: List[str] = []
    if step_formset.is_valid():
        for f in step_formset:
            if f.cleaned_data.get("DELETE", False):
                continue
            text = (f.cleaned_data.get("text") or "").strip()
            if not text:
                f.add_error("text", "Шаг не может быть пустым.")
            else:
                steps.append(text)
        if not steps and step_formset.forms:
            step_formset.forms[0].add_error(None, "Добавьте хотя бы один шаг.")
            valid = False
    else:
        valid = False

    if not valid:
        ctx = {
            "recipe_form": form,
            "ing_formset": ing_formset,
            "step_formset": step_formset,
            "upload_form": UploadForm(),
            "files_json": _list_files("json"),
            "files_xml": _list_files("xml"),
            "data_dir": str(getattr(settings, "DATA_DIR", settings.BASE_DIR / "data")),
        }
        return render(request, "recepie/index.html", ctx)

    data = {
        "title": form.cleaned_data["title"].strip(),
        "servings": form.cleaned_data["servings"],
        "ingredients": ingredients,
        "steps": steps,
    }

    # Всегда сохраняем в XML
    name = _safe_name("xml")
    path = _data_dir("xml") / name
    raw = _to_xml(_validate_recipe_dict(data))
    path.write_bytes(raw)

    messages.success(request, f"Файл (XML) сохранён: {escape(name)}")
    return redirect("index")


@require_http_methods(["POST"])
def upload_file(request: HttpRequest) -> HttpResponse:
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Не выбран файл.")
        return redirect("index")
    up = form.cleaned_data["file"]
    content_type = (up.content_type or "").lower()
    if content_type in ("application/xml", "text/xml") or up.name.lower().endswith(".xml"):
        fmt = "xml"
    else:
        messages.error(request, "Поддерживаются только XML.")
        return redirect("index")

    safe_name = _safe_name(fmt)
    target = _data_dir(fmt) / safe_name
    with target.open("wb") as dst:
        for chunk in up.chunks():
            dst.write(chunk)

    try:
        raw = target.read_bytes()
        data = _from_json(raw) if fmt == "json" else _from_xml(raw)
        _validate_recipe_dict(data)
    except Exception as e:
        target.unlink(missing_ok=True)
        messages.error(request, f"Файл отклонён: {escape(str(e))}. Файл удалён.")
        return redirect("index")

    messages.success(request, f"Файл загружен и валиден: {escape(safe_name)}")
    return redirect("index")


@require_http_methods(["GET"])
def view_file(request: HttpRequest, fmt: str, fname: str) -> HttpResponse:
    fmt = (fmt or "").lower()
    if fmt not in ("xml"):
        raise Http404("Неверный формат.")
    raw = _read_file(fmt, fname)
    try:
        data = _from_json(raw) if fmt == "json" else _from_xml(raw)
        data = _validate_recipe_dict(data)
    except Exception:
        pre = raw.decode("utf-8", errors="replace")
        return render(request, "recepie/view.html", {"fname": fname, "data": None, "raw": pre})

    return render(request, "recepie/view.html", {"fname": fname, "data": data, "raw": None})