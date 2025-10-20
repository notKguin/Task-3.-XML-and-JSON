# recipes/templatetags/recipe_extras.py
from django import template
register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Позволяет обращаться к словарю по ключу: {{ dict|get_item:key }}"""
    if dictionary and key in dictionary:
        return dictionary[key]
    return ""
