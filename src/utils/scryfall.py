"""
FUNCTIONS THAT INTERACT WITH SCRYFALL
"""
# Standard Library Imports
import os
import json
from pathlib import Path
from shutil import copyfileobj
from typing import Optional, Union, Callable, Any

# Third Party Imports
import requests
from ratelimit import sleep_and_retry, RateLimitDecorator
from backoff import on_exception, expo

# Local Imports
from src.enums.mtg import TransformIcons
from src.console import console
from src.settings import cfg
from src.constants import con
from src.types.cards import CardDetails
from src.utils.exceptions import ScryfallError
from src.utils.files import load_data_file, dump_data_file
from src.utils.regex import Reg
from src.utils.strings import msg_warn, normalize_str


"""
* Relevant Data
"""

# Scryfall API entrypoints
SCRY_API_SETS = 'https://api.scryfall.com/sets'
SCRY_API_CARDS = 'https://api.scryfall.com/cards'
SCRY_API_CARDS_SEARCH = 'https://api.scryfall.com/cards/search'

# MTGJSON API entrypoints
MTGJSON_API = 'https://mtgjson.com/api/v5'

# Data to remove from MTGJSON set data
MTGJSON_SET_DATA_EXTRA = [
    'sealedProduct',
    'booster',
    'cards'
]

"""
ERROR HANDLING
"""


# RateLimiter object to handle Scryfall rate limits
scryfall_rate_limit = RateLimitDecorator(calls=20, period=1)


def handle_final_exception(fail_response: Optional[Any]) -> Callable:
    """
    Decorator to handle any exception and return appropriate failure value.
    @param fail_response: Return value if Exception occurs.
    @return: Return value of the function, or fail_response.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Final exception catch
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # All requests failed
                console.log_exception(e)
                if fail_response == 'error':
                    # Return formatted Scryfall Error
                    return ScryfallError()
                return fail_response
        return wrapper
    return decorator


def handle_request_failure(
    fail_response: Optional[Any] = 'error'
) -> Callable:
    """
    Decorator to handle all Scryfall request failure cases, and return appropriate failure value.
    @param fail_response: The value to return if request failed entirely. By default, it
                          tries to return a ScryfallError formatting proper failure message.
    @return: Requested data if successful, fail_response if not.
    """
    def decorator(func):
        @sleep_and_retry
        @scryfall_rate_limit
        @on_exception(expo, requests.exceptions.RequestException, max_tries=3, max_time=1)
        @handle_final_exception(fail_response)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


"""
INTERMEDIARIES
"""


def get_card_data(
    card_name: str,
    card_set: Optional[str] = None,
    card_number: Optional[str] = None
) -> Union[dict, Exception]:
    """
    Fetch card data from Scryfall API.
    @param card_name: Name of the card.
    @param card_set: Set code of the card.
    @param card_number: Collector number of the card.
    @return: Scryfall dict or Exception.
    """

    # Establish Scryfall fetch action
    name_normalized = normalize_str(card_name, True)
    action = get_card_unique if card_number else get_card_search
    params = [card_set, str(card_number).lstrip('0 ')] if card_number else [card_name, card_set]

    # Query the card in alternate language
    if cfg.lang != "en":
        card = action(*params, lang=cfg.lang)

        # Was the result correct?
        if isinstance(card, dict):
            card['name_normalized'] = name_normalized
            return process_scryfall_data(card)
        elif not cfg.test_mode:
            # Language couldn't be found
            console.update(msg_warn(f"Reverting to English: [b]{card_name}[/b]"))

    # Query the card in English, retry with extras if failed
    card = action(*params)
    if not isinstance(card, dict) and not cfg.scry_extras:
        card = action(*params, extras=True)
    # Return valid card or return Exception
    if isinstance(card, dict):
        card['name_normalized'] = name_normalized
        return process_scryfall_data(card)
    return card


def get_set_data(card_set: str) -> Optional[dict]:
    """
    Grab available set data.
    @param card_set: The set to look for, ex: MH2
    @return: MTG set dict or empty dict.
    """
    # Has this set been logged?
    path = Path(con.path_data_sets, f"SET-{card_set.upper()}.json")
    if os.path.exists(path):
        try:
            # Try to load existing data file
            data = load_data_file(path)
            if 'scryfall' in data:
                return data
        except Exception as e:
            console.log_exception(e)

    # Get Scryfall data, then check for token set
    data_scry = get_set_scryfall(card_set)
    if data_scry.get('set_type', '') == 'token':
        card_set = data_scry.get('parent_set_code', card_set)

    # Get MTGJSON data and fold it in
    data_mtg = get_set_mtgjson(card_set)
    data_scry.update(data_mtg)

    # Save the data if both lookups were valid, or 'printed_size' is present
    if (data_mtg and data_scry) or 'printed_size' in data_scry:
        try:
            # Try to dump set data
            dump_data_file(data_scry, path)
        except Exception as e:
            console.log_exception(e)

    # Enforce valid data
    return data_scry if isinstance(data_scry, dict) else {}


"""
REQUEST FUNCTIONS
"""


@handle_request_failure()
def get_card_unique(
    card_set: str,
    card_number: str,
    lang: str = 'en'
) -> Union[dict, ScryfallError]:
    """
    Get card using /cards/:code/:number(/:lang) Scryfall API endpoint.
    @note: https://scryfall.com/docs/api/cards/collector
    @param card_set: Set code of the card, ex: MH2
    @param card_number: Collector number of the card
    @param lang: Lang code to look for, ex: en
    @return: Card dict or ScryfallError
    """
    lang = '' if lang == 'en' else f'/{lang}'
    res = requests.get(
        url=f'{SCRY_API_CARDS}/{card_set.lower()}/{card_number}{lang}',
        headers=con.http_header
    )
    card, url = res.json(), res.url

    # Ensure playable card was returned
    if card.get('object') != 'error' and check_playable_card(card):
        return card
    return ScryfallError(url, code=card_set, number=card_number, lang=lang)


@handle_request_failure()
def get_card_search(
    card_name: str,
    card_set: Optional[str] = None,
    lang: str = 'en',
    extras: bool = False
) -> Union[dict, ScryfallError]:
    """
    Get card using /cards/search Scryfall API endpoint.
    @note: https://scryfall.com/docs/api/cards/search
    @param card_name: Name of the card, ex: Damnation
    @param card_set: Set code to look for, ex: MH2
    @param lang: Lang code to look for, ex: en
    @param extras: Forces include_extras if True, otherwise use setting.
    @return: Card dict or ScryfallError
    """
    # Query Scryfall
    res = requests.get(
        url = SCRY_API_CARDS_SEARCH,
        headers=con.http_header,
        params={
            'unique': cfg.scry_unique,
            'order': cfg.scry_sorting,
            'dir': 'asc' if cfg.scry_ascending else 'desc',
            'include_extras': extras if extras else cfg.scry_extras,
            'q': f'!"{card_name}"'
                 f" lang:{lang}"
                 f"{f' set:{card_set.lower()}' if card_set else ''}"})

    # Card data returned, Scryfall encoded URL
    card, url = res.json() or {}, res.url

    # Check for a playable card
    for c in card.get('data', []):
        if check_playable_card(c):
            return c

    # No playable results
    return ScryfallError(url, name=card_name, code=card_set, lang=lang)


@handle_request_failure([])
def get_cards_paged(url: str = SCRY_API_CARDS_SEARCH, all_pages: bool = True, **kwargs) -> list[dict]:
    """
    Grab paginated card list from a Scryfall API endpoint.
    @param url: Scryfall API URL endpoint to access.
    @param all_pages: Whether to return all additional pages, or just the first.
    @param kwargs: Optional parameters to pass to API endpoint.
    """
    # Query Scryfall
    res = requests.get(url=url, headers=con.http_header, params=kwargs).json()
    cards = res.get('data', [])

    # Add additional pages if any exist
    if all_pages and res.get("has_more") and res.get("next_page"):
        cards.extend(
            get_cards_paged(
                url=res.get['next_page'],
                all_pages=all_pages
            ))
    return cards


@handle_request_failure([])
def get_cards_oracle(oracle_id: str, all_pages: bool = False, **kwargs) -> list[dict]:
    """
    Grab paginated card list from a Scryfall API endpoint using the Oracle ID of the card.
    @param oracle_id: Scryfall Oracle ID of the card.
    @param all_pages: Whether to return all additional pages, or just the first.
    @param kwargs: Optional parameters to pass to API endpoint.
    """
    return get_cards_paged(
        url=SCRY_API_CARDS_SEARCH,
        all_pages=all_pages,
        **{
            'q': f'oracleid:{oracle_id}',
            'dir': kwargs.pop('dir', 'asc'),
            'order': kwargs.pop('order', 'released'),
            'unique': kwargs.pop('unique', 'prints'),
            **kwargs
        })


@handle_request_failure({})
def get_set_mtgjson(card_set: str) -> dict:
    """
    Grab available set data from MTG Json.
    @param card_set: The set to look for, ex: MH2
    @return: MTGJson set dict or empty dict.
    """
    # Grab from MTG JSON
    j = requests.get(
        f"{MTGJSON_API}/{card_set.upper()}.json",
        headers=con.http_header
    ).json().get('data', {})

    # Add token count if tokens present
    j['tokenCount'] = len(j.pop('tokens', []))

    # Remove unneeded data
    [j.pop(n) for n in MTGJSON_SET_DATA_EXTRA]

    # Return data if valid
    return j if j.get('name') else {}


@handle_request_failure({})
def get_set_scryfall(card_set: str) -> dict:
    """
    Grab available set data from MTG Json.
    @param card_set: The set to look for, ex: MH2
    @return: Scryfall set dict or empty dict.
    """
    # Grab from Scryfall
    source = requests.get(
        f"{SCRY_API_SETS}/{card_set.upper()}",
        headers=con.http_header
    ).text
    j = json.loads(source)

    # Return data if valid
    j.setdefault('scryfall', True)
    return j if j.get('name') else {}


@handle_request_failure(None)
def card_scan(img_url: str) -> Optional[str]:
    """
    Downloads scryfall art from URL
    @param img_url: Scryfall URI for image.
    @return: Filename of the saved image, None if unsuccessful.
    """
    r = requests.get(img_url, stream=True)
    with open(con.path_scryfall_scan, 'wb') as f:
        copyfileobj(r.raw, f)
        return f.name


"""
CARD DATA UTILITIES
"""


def parse_card_info(file_path: Path) -> CardDetails:
    """
    Retrieve card name from the input file, and optional tags (artist, set, number).
    @param file_path: Path to the image file.
    @return: Dict of card details.
    """
    # Extract just the card name
    file_name = file_path.stem

    # Match pattern and format data
    name_split = Reg.PATH_SPLIT.split(file_name)
    artist = Reg.PATH_ARTIST.search(file_name)
    number = Reg.PATH_NUM.search(file_name)
    code = Reg.PATH_SET.search(file_name)

    # Return dictionary
    return {
        'filename': file_path,
        'name': name_split[0].strip(),
        'set': code.group(1) if code else '',
        'artist': artist.group(1) if artist else '',
        'number': number.group(1) if number and code else '',
        'creator': name_split[-1] if '$' in file_name else '',
    }


def check_playable_card(card_json: dict) -> bool:
    """
    Checks if this card object is a playable game piece.
    @param card_json: Scryfall data for this card.
    @return: Valid scryfall data if check passed, else None.
    """
    if card_json.get('set_type') in ["minigame"]:
        return False
    if card_json.get('layout') in ['art_series', 'reversible_card']:
        return False
    return True


def process_scryfall_data(data: dict) -> dict:
    """
    Process any additional required data before sending it to the layout object.
    @param data: Unprocessed scryfall data.
    @return: Processed scryfall data.
    """
    # Modify meld card data to fit transform layout
    if data['layout'] == 'meld':
        # Ignore tokens and other objects
        front, back = [], None
        for part in data.get('all_parts', []):
            if part.get('component') == 'meld_part':
                front.append(part)
            if part.get('component') == 'meld_result':
                back = part

        # Figure out if card is a front or a back
        faces = [front[0], back] if (
            data['name_normalized'] == normalize_str(back['name'], True) or
            data['name_normalized'] == normalize_str(front[0]['name'], True)
        ) else [front[1], back]

        # Pull JSON data for each face and set object to card_face
        data['card_faces'] = [
            {**requests.get(n['uri'], headers=con.http_header).json(), 'object': 'card_face'}
            for n in faces
        ]

        # Add meld transform icon if none provided
        if not any([bool(n in TransformIcons) for n in data.get('frame_effects', [])]):
            data.setdefault('frame_effects', []).append(TransformIcons.MELD)
        data['layout'] = 'transform'

    # Check for alternate MDFC / Transform layouts
    if 'card_faces' in data:
        # Select the corresponding face
        card = data['card_faces'][0] if (
            normalize_str(data['card_faces'][0]['name'], True) == data['name_normalized']
        ) else data['card_faces'][1]
        # Transform / MDFC Planeswalker layout
        if 'Planeswalker' in card['type_line']:
            data['layout'] = 'planeswalker_tf' if data['layout'] == 'transform' else 'planeswalker_mdfc'
        # Transform Saga layout
        if 'Saga' in card['type_line']:
            data['layout'] = 'saga'
        # Battle layout
        if 'Battle' in card['type_line']:
            data['layout'] = 'battle'
        return data

    # Add Mutate layout
    if 'Mutate' in data.get('keywords', []):
        data['layout'] = 'mutate'
        return data

    # Add Planeswalker layout
    if 'Planeswalker' in data.get('type_line', ''):
        data['layout'] = 'planeswalker'
        return data

    # Return updated data
    return data
