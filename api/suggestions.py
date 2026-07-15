"""Message autocomplete using pg_trgm similarity on MessageSuggestion.
No external API, no ML model — ~0 MB RAM overhead, ~0% CPU, <5ms per query."""
from django.contrib.postgres.search import TrigramWordSimilarity
from django.db.models import F
from .models import MessageSuggestion

TAG_BOOST = 0.10
MIN_SIMILARITY = 0.15
MAX_ROWS = 50_000


def search(partial_text, conversation_tags=None, top_k=3):
    """Return best completion + alternatives for partial_text."""
    if len(partial_text.strip()) < 2:
        return None

    tag_set = set(conversation_tags or [])

    qs = (
        MessageSuggestion.objects
        .annotate(sim=TrigramWordSimilarity(partial_text, 'text'))
        .filter(sim__gt=MIN_SIMILARITY)
        .order_by(F('sim').desc(nulls_last=True),
                  F('usage_count').desc(nulls_last=True))
    )

    results = list(qs[:top_k + 5])
    if not results:
        return None

    scored = []
    for r in results:
        score = float(r.sim or 0)
        score += len(tag_set & set(r.tags or [])) * TAG_BOOST
        scored.append((score, r.text))

    scored.sort(key=lambda x: x[0], reverse=True)

    if scored[0][0] < MIN_SIMILARITY:
        return None

    return {
        'suggestion': scored[0][1],
        'alternatives': [t for _, t in scored[1:top_k]],
    }


def index_message(text, tags=None, agent_id=None):
    """Upsert a sent message text into the suggestion corpus."""
    if not text or not text.strip():
        return

    obj, created = MessageSuggestion.objects.get_or_create(
        text=text.strip(),
        defaults={'tags': tags or [], 'agent_id': agent_id},
    )
    if not created:
        obj.usage_count = F('usage_count') + 1
        obj.tags = tags or []
        obj.save(update_fields=['usage_count', 'tags', 'last_used'])

    # Cap at MAX_ROWS — delete least-used overflow
    count = MessageSuggestion.objects.count()
    if count > MAX_ROWS:
        keep = list(
            MessageSuggestion.objects
            .order_by('-usage_count', '-last_used')
            .values_list('id', flat=True)[:MAX_ROWS]
        )
        MessageSuggestion.objects.exclude(id__in=keep).delete()
