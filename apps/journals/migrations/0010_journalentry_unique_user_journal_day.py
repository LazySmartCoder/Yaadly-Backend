from django.db import migrations, models
from django.db.models import Count


def merge_duplicate_day_entries(apps, schema_editor):
    """Collapse any existing multiple entries for the same (user, date).

    The oldest entry becomes the day's single entry; later entries have their
    text folded into it, their photos re-pointed, and any chat session link
    moved across before they are deleted.
    """
    JournalEntry = apps.get_model("journals", "JournalEntry")
    Gallery = apps.get_model("journals", "Gallery")
    ChatSession = apps.get_model("journals", "ChatSession")

    duplicate_days = (
        JournalEntry.objects.values("user_id", "date")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )

    for group in duplicate_days:
        entries = list(
            JournalEntry.objects.filter(
                user_id=group["user_id"], date=group["date"]
            ).order_by("created_at", "id")
        )
        keep = entries[0]
        contents = [(keep.content or "").strip()]

        for extra in entries[1:]:
            text = (extra.content or "").strip()
            if text and text not in contents:
                contents.append(text)
            Gallery.objects.filter(entry_id=extra.id).update(entry_id=keep.id)
            ChatSession.objects.filter(journal_entry_id=extra.id).update(
                journal_entry_id=keep.id
            )
            extra.delete()

        keep.content = "\n\n".join(part for part in contents if part)
        keep.save(update_fields=["content"])


class Migration(migrations.Migration):

    dependencies = [
        ("journals", "0009_remove_journalentry_aid"),
    ]

    operations = [
        migrations.RunPython(
            merge_duplicate_day_entries,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="journalentry",
            constraint=models.UniqueConstraint(
                fields=("user", "date"),
                name="unique_user_journal_day",
            ),
        ),
    ]
