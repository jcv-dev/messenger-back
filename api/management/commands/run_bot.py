from django.core.management.base import BaseCommand
from api.bot.dispatcher import run


class Command(BaseCommand):
    help = "Run the WhatsApp bot worker"

    def handle(self, *args, **options):
        self.stdout.write("Starting bot worker...")
        run()
