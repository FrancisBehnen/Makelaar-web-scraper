"""The aanmeldbrief (application letter) and e-mail subject for a listing."""

ADRES_PLACEHOLDER = "[[ADRES]]"

AANMELDBRIEF_TEMPLATE = (
    "Geachte meneer, mevrouw,\n\n"
    f"Zojuist zagen wij jullie woning aan de {ADRES_PLACEHOLDER}. Mijn partner "
    "Francis en ik, Corlien, zijn op zoek naar een eerste thuis om in te gaan "
    "samenwonen, nu wij allebei onderhand zijn afgestudeerd en aan banen zijn "
    "gestart. We hebben elkaar leren kennen via de Delftse "
    "studentenzeilvereniging en zijn zo verliefd geworden op Delft, dat we "
    "hier in de regio zouden willen blijven wonen. We zien hier ons al "
    "helemaal wonen! Graag zouden wij ons daarom aan willen melden voor de "
    "bezichtiging van het appartement.\n\n"
    "Zelf ben ik recent afgestudeerd scheikundige en start ik deze maand als "
    "junior chemicus bij Lignitec, een Delftse startup in biobouwmaterialen. "
    "Daarnaast ben ik al een aantal jaar werkzaam als retailspecialist bij "
    "Sounds, een platenzaak in het centrum van Delft. Daar ben ik tijdens mijn "
    "studententijd terecht gekomen als bijbaan, omdat ik al jaren LP's "
    "verzamel. Daar blijf ik nog part-time werkzaam. Mijn inkomen zit vanaf "
    "deze maand gecombineerd tussen de €2000 - €2500 per maand.\n\n"
    "Mijn partner Francis werkt als AI-specialist bij Coolblue in Rotterdam, "
    "waar hij met veel enthousiasme werkt aan de toekomst van online retail. "
    "In 2024 heeft hij zijn master Technische Informatica (Computer Science) "
    "afgerond aan de TU Delft. Als hij thuiskomt van werk, vindt hij het leuk "
    "om lekker te koken. Hij verdient tussen de €3000 - €3500 per maand.\n\n"
    "Wij zouden graag de woning komen bezichtigen. Zou u ons kunnen laten "
    "weten wanneer de bezichtiging is en of wij zouden mogen komen? We zien "
    "uit naar uw reactie! Bij voorbaat hartelijk dank voor uw tijd.\n\n"
    "Met vriendelijke groet,\n"
    "Corlien Douma\n"
    "+31646853193"
)


def aanmeldbrief(straatnaam_huisnummer: str) -> str:
    """Plain-text letter for an address (no Telegram markup)."""
    return AANMELDBRIEF_TEMPLATE.replace(ADRES_PLACEHOLDER, straatnaam_huisnummer)


def subject(straatnaam_huisnummer: str, plaats: str) -> str:
    if plaats:
        return f"Reactie op {straatnaam_huisnummer}, {plaats}"
    return f"Reactie op {straatnaam_huisnummer}"


def escape_html(text: str) -> str:
    """Escape the characters that are special inside Telegram HTML messages."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
