"""Static banter snippets. Picked randomly before each track announcement."""
import random
from typing import Optional

# Keyed by genre (lowercase). Falls back to GENERIC if no match.
_GENRE_BANTER: dict[str, list[str]] = {
    "rock": [
        "Buckle up — this one's got some serious riff energy.",
        "Turn it up. Your neighbours have headphones.",
        "Nothing fixes a bad day like a power chord.",
        "This track was basically made for driving too fast.",
    ],
    "jazz": [
        "Let the syncopation wash over you.",
        "Close your eyes. You're in a smoky club. It's 1957.",
        "Jazz: the sound of musicians having more fun than you.",
        "This one swings harder than a playground in a hurricane.",
    ],
    "classical": [
        "Clear your mind. This one demands your full attention.",
        "No words needed. Just listen.",
        "Music that was already ancient when your grandparents were born.",
        "Centuries old and still hits different.",
    ],
    "pop": [
        "You're going to be humming this for the rest of the week.",
        "Infectious hooks incoming. You've been warned.",
        "This one goes straight to the chorus. No patience required.",
    ],
    "hip-hop": [
        "Bars. Straight bars.",
        "This beat alone is worth the price of admission.",
        "Lyricism and rhythm in perfect balance.",
    ],
    "electronic": [
        "Four on the floor. Let's go.",
        "Your brain on synthesisers.",
        "This one was built in a laptop and it still slaps.",
    ],
    "metal": [
        "Volume up. Way up.",
        "This is not background music. This is foreground music.",
        "The guitars are angry and that's a feature, not a bug.",
    ],
}

_GENERIC: list[str] = [
    "Here's one that deserves your full attention.",
    "A track that holds up no matter how many times you've heard it.",
    "Coming up next — no skipping allowed.",
    "This one's a keeper.",
    "Sit back. Let it play.",
    "You picked a great genre. This proves it.",
    "Queued up and ready to go.",
]


def get_banter(genre: Optional[str] = None) -> str:
    if genre:
        pool = _GENRE_BANTER.get(genre.lower(), _GENERIC)
    else:
        pool = _GENERIC
    return random.choice(pool)
