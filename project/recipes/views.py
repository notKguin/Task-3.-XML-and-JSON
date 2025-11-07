import os
import uuid
from xml.etree import ElementTree as ET
from django.shortcuts import render, redirect
from django.conf import settings
from django.core.files.storage import default_storage
from .models import Recipe

from django.template.defaulttags import register

@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)


XML_DIR = os.path.join(settings.MEDIA_ROOT, 'recipes')
XML_PATH = os.path.join(XML_DIR, 'recipes.xml')


def ensure_dir():
    os.makedirs(XML_DIR, exist_ok=True)


def save_to_xml():
    """Сохраняет все рецепты из базы в единый файл XML"""
    ensure_dir()
    root = ET.Element("recipes")
    for r in Recipe.objects.all():
        recipe_el = ET.SubElement(root, "recipe")
        for field in Recipe._meta.fields:
            if field.name == "id":
                continue
            ET.SubElement(recipe_el, field.name).text = str(getattr(r, field.name, "") or "")
    tree = ET.ElementTree(root)
    tree.write(XML_PATH, encoding="utf-8", xml_declaration=True)


def import_from_xml(file_path):
    """Импорт рецептов из загруженного XML файла"""
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()

        # Проверяем структуру
        if root.tag != "recipes":
            raise ValueError("Некорректный корневой элемент (ожидался <recipes>).")

        imported = 0
        for idx, el in enumerate(root.findall("recipe"), start=1):
            data = {}
            for field in Recipe._meta.fields:
                if field.name == "id":
                    continue
                node = el.find(field.name)
                if node is None or (node.text or "").strip() == "":
                    raise ValueError(f"Ошибка в рецепте №{idx}: отсутствует тег <{field.name}>.")
                data[field.name] = node.text.strip()
            Recipe.objects.create(**data)
            imported += 1
        return True, f"Импортировано {imported} рецептов."

    except Exception as e:
        return False, f"Ошибка при импорте: {e}"


def index(request):
    ensure_dir()
    message = ""
    fields = [f for f in Recipe._meta.fields if f.name != "id"]

    # Добавление рецепта вручную
    if request.method == "POST" and "add_recipe" in request.POST:
        data = {f.name: request.POST.get(f.name, "") for f in fields}
        Recipe.objects.create(**data)
        save_to_xml()
        return redirect("index")

    # Загрузка XML-файла
    if request.method == "POST" and "upload_xml" in request.POST:
        uploaded = request.FILES.get("xml_file")
        if not uploaded:
            message = "Файл не выбран."
        else:
            # Генерация безопасного имени
            safe_name = f"upload_{uuid.uuid4().hex}.xml"
            upload_path = os.path.join(XML_DIR, safe_name)
            ensure_dir()
            with default_storage.open(upload_path, "wb+") as destination:
                for chunk in uploaded.chunks():
                    destination.write(chunk)

            # Проверка валидности
            ok, msg = import_from_xml(upload_path)
            if not ok:
                os.remove(upload_path)
            else:
                os.remove(upload_path)
                save_to_xml()
            message = msg

    # Получаем данные из базы
    recipes = [
        {f.name: getattr(r, f.name, "") for f in fields}
        for r in Recipe.objects.all()
    ]
    xml_exists = os.path.exists(XML_PATH)

    return render(request, "recipes/index.html", {
        "fields": fields,
        "recipes": recipes,
        "xml_exists": xml_exists,
        "xml_path": XML_PATH.replace(settings.MEDIA_ROOT, settings.MEDIA_URL),
        "message": message,
    })
