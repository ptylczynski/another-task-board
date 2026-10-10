"""Emoji choices for board icons, with searchable names."""

import random
import unicodedata

# Single-codepoint emoji only, so Unicode names are available without an extra dependency.
_EMOJIS = (
    # Work and planning
    "📋📌📎📁📂📅📆📈📊📝📚📖🔖🎓💼💡🔍🔑🔒🔧🔨🧰🧪🧭🎯🏆🏁🚀💻📱📷📦📮📣🔔⏰⌛"
    # Home and life
    "🏠🏡🛒🛏🛋🧹🧺🧼🪴🍳🍽🎁🎂🎈🎉🎨🎬🎮🎲🎵🎸🎧📺👶👪🐶🐱🐟🐦"
    # Travel and places
    "✈🚗🚲🚆🚢🗺🏖🏔🏕🏙🌍🌋🗽⛺"
    # Nature and weather
    "🌱🌲🌳🌴🌵🌷🌸🌹🌻🌼🍀🍁🍂🍄🌙🌞🌈🌊🔥⚡⛄💧"
    # Food and drink
    "🍎🍊🍋🍌🍉🍇🍓🍒🥑🥕🌽🍕🍔🍟🌮🍜🍣🍩🍪🍰🍫☕🍵🍷🍺"
    # Sport and health
    "⚽🏀🏈⚾🎾🏐🏓🏸🏊🏃🚴🧘💪🏋🥇💊🩺"
    # Symbols
    "⭐🌟✨💎💰💳🧠❤💙💚💛💜🧡🖤🔴🟠🟡🟢🔵🟣⚪⚫"
)

# Friendlier names where the official Unicode name is odd or unhelpful for searching.
_NAME_OVERRIDES = {
    "❤": "red heart",
    "🏠": "house home",
    "🏡": "house with garden home",
    "💼": "briefcase work",
    "🛒": "shopping cart groceries",
    "✈": "airplane travel flight",
    "🏋": "weight lifter gym",
    "🧘": "person in lotus position yoga",
    "💪": "flexed biceps strong fitness",
    "🩺": "stethoscope health doctor",
    "💊": "pill medicine health",
    "💰": "money bag finance",
    "💳": "credit card payment",
    "🎯": "direct hit target goal",
    "🚀": "rocket launch project",
    "🧠": "brain idea learning",
    "📚": "books reading learning",
    "🎓": "graduation cap school",
}

# These default to monochrome text glyphs; the variation selector asks for the color emoji.
_TEXT_DEFAULT = set("✈❤🛏🛋🗺🏔🏕🏙🏖🏋")

EMOJI_NAMES = {
    emoji + ("\ufe0f" if emoji in _TEXT_DEFAULT else ""): _NAME_OVERRIDES.get(
        emoji, unicodedata.name(emoji).lower()
    )
    for emoji in dict.fromkeys(_EMOJIS)  # dedupe, keep order
}
EMOJIS = list(EMOJI_NAMES)


def emoji_label(emoji):
    return f"{emoji}  {EMOJI_NAMES.get(emoji, '')}"


def random_emoji():
    return random.choice(EMOJIS)
