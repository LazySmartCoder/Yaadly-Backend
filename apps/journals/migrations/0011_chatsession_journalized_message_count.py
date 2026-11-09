from django.db import migrations, models


def mark_existing_finalized_messages(apps, schema_editor):
    ChatSession = apps.get_model("journals", "ChatSession")
    for session in ChatSession.objects.filter(status="finalized").iterator():
        session.journalized_message_count = len(session.messages or [])
        session.save(update_fields=["journalized_message_count"])


class Migration(migrations.Migration):

    dependencies = [
        ("journals", "0010_journalentry_unique_user_journal_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatsession",
            name="journalized_message_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(
            mark_existing_finalized_messages,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
