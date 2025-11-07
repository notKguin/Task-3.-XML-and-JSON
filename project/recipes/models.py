from django.db import models

class Recipe(models.Model):
    title = models.CharField('Название', max_length=200)
    description = models.TextField('Описание', blank=True)
    ingredients = models.TextField('Ингредиенты', blank=True)
    steps = models.TextField('Шаги', blank=True)
    colories = models.BooleanField(("Диетическое"))

    def __str__(self):
        return self.title
