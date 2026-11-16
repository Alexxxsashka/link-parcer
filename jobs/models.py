from django.db import models


class SearchQuery(models.Model):
    keywords = models.CharField("Запрос", max_length=200)
    country = models.CharField("Страна", max_length=100)
    enabled = models.BooleanField("Активен", default=True)

    class Meta:
        ordering = ("keywords", "country")
        verbose_name = "Поисковый запрос"
        verbose_name_plural = "Поисковые запросы"
        constraints = [
            models.UniqueConstraint(fields=("keywords", "country"), name="unique_search_query"),
        ]

    def __str__(self):
        return f"{self.keywords} · {self.country}"


class Company(models.Model):
    source_key = models.CharField("Идентификатор", max_length=255, unique=True)
    name = models.TextField("Название", blank=True)
    profile_url = models.URLField("Профиль LinkedIn", max_length=1000, blank=True)
    data = models.JSONField("Данные", default=dict, blank=True)

    class Meta:
        verbose_name = "Компания"
        verbose_name_plural = "Компании"

    def __str__(self):
        return self.name or self.source_key


class Job(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает обработки"
        DONE = "done", "Обработана"
        RETRY = "retry", "Временная ошибка"
        UNAVAILABLE = "unavailable", "Недоступна"

    source_id = models.CharField("ID LinkedIn", max_length=32, unique=True)
    url = models.URLField("Вакансия LinkedIn", max_length=1000)
    title = models.TextField("Заголовок")
    listed_company = models.TextField("Компания в выдаче", blank=True)
    listed_company_url = models.URLField("Профиль компании в выдаче", max_length=1000, blank=True)
    location = models.TextField("Местоположение", blank=True)
    searches = models.ManyToManyField(SearchQuery, related_name="jobs", verbose_name="Запросы")
    company = models.ForeignKey(
        Company, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Компания",
    )
    details = models.JSONField("Подробности вакансии", default=dict, blank=True)
    status = models.CharField("Статус", max_length=16, choices=Status, default=Status.PENDING)
    attempts = models.PositiveIntegerField("Попытки", default=0)
    attempted_at = models.DateTimeField("Последняя попытка", null=True, blank=True)
    error = models.TextField("Ошибка", blank=True)

    class Meta:
        ordering = ("-id",)
        indexes = [models.Index(fields=("status", "id"))]
        verbose_name = "Вакансия"
        verbose_name_plural = "Вакансии"

    def __str__(self):
        return self.title


class Run(models.Model):
    stage = models.CharField("Этап", max_length=10)
    query = models.ForeignKey(
        SearchQuery, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Запрос",
    )
    parameters = models.JSONField("Параметры", default=dict)
    started_at = models.DateTimeField("Начало", auto_now_add=True)
    finished_at = models.DateTimeField("Конец", null=True, blank=True)
    reason = models.CharField("Причина завершения", max_length=100)
    counts = models.JSONField("Счётчики", default=dict)
    error = models.TextField("Ошибка", blank=True)

    class Meta:
        ordering = ("-id",)
        verbose_name = "Прогон"
        verbose_name_plural = "Прогоны"
