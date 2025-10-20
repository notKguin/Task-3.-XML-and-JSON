import os
from xml.etree import ElementTree as ET
from django.shortcuts import render, redirect
from django.conf import settings
from .models import Recipe

XML_DIR = os.path.join(settings.MEDIA_ROOT, 'recipes')
XML_PATH = os.path.join(XML_DIR, 'recipes.xml')


def ensure_dir():
    os.makedirs(XML_DIR, exist_ok=True)


def save_to_xml():
    """Сохраняет все рецепты в recipes.xml"""
    ensure_dir()
    root = ET.Element("recipes")

    for r in Recipe.objects.all():
        recipe_el = ET.SubElement(root, "recipe")
        for field in Recipe._meta.fields:
            if field.name == "id":
                continue
            value = getattr(r, field.name, "") or ""
            ET.SubElement(recipe_el, field.name).text = str(value)

    tree = ET.ElementTree(root)
    tree.write(XML_PATH, encoding="utf-8", xml_declaration=True)


def format_multiline_field(text: str, mode: str) -> str:
    """Форматирует многострочные поля"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""

    if mode == "ingredients":
        # Добавляем "-" перед каждой строкой
        return "\n".join(f"- {l}" for l in lines)
    elif mode == "steps":
        # Нумерация 1., 2., 3.
        return "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))
    return "\n".join(lines)


def index(request):
    ensure_dir()
    fields = [f for f in Recipe._meta.fields if f.name != "id"]

    if request.method == "POST":
        data = {f.name: request.POST.get(f.name, "") for f in fields}

        # 🔹 Автоматическое форматирование
        if "ingredients" in data:
            data["ingredients"] = format_multiline_field(data["ingredients"], "ingredients")
        if "steps" in data:
            data["steps"] = format_multiline_field(data["steps"], "steps")

        Recipe.objects.create(**data)
        save_to_xml()
        return redirect("index")

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
    })
