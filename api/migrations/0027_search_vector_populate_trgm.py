# Generated manually — populate search_vector, enable pg_trgm, add trigger + fuzzy index

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0026_agent_presence_push_subscription_search'),
    ]

    operations = [
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            reverse_sql="DROP EXTENSION IF EXISTS pg_trgm",
        ),
        migrations.RunSQL(
            "UPDATE api_message SET search_vector = to_tsvector('spanish', coalesce(content, '')) WHERE search_vector IS NULL",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            """
            CREATE OR REPLACE FUNCTION message_search_vector_update() RETURNS trigger AS $$
            BEGIN
                NEW.search_vector = to_tsvector('spanish', coalesce(NEW.content, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """,
            reverse_sql="DROP FUNCTION IF EXISTS message_search_vector_update() CASCADE",
        ),
        migrations.RunSQL(
            """
            CREATE TRIGGER message_search_vector_trigger
            BEFORE INSERT OR UPDATE OF content ON api_message
            FOR EACH ROW EXECUTE FUNCTION message_search_vector_update()
            """,
            reverse_sql="DROP TRIGGER IF EXISTS message_search_vector_trigger ON api_message",
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS msg_content_trgm_idx ON api_message USING gin (content gin_trgm_ops)",
            reverse_sql="DROP INDEX IF EXISTS msg_content_trgm_idx",
        ),
    ]
