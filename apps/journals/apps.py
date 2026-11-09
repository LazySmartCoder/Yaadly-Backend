import sys

from django.apps import AppConfig


class JournalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.journals"
    verbose_name = "Journals"

    def ready(self):
        command = " ".join(sys.argv).lower()
        if "test" in command:
            return
        if "runserver" in command and "gunicorn" not in command:
            import os

            if os.environ.get("RUN_MAIN") != "true":
                return
        elif "gunicorn" not in command:
            return

        from .ollama import warm_model

        warm_model()
