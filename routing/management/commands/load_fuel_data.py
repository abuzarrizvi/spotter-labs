import csv
import re
import time
import requests
from django.core.management.base import BaseCommand
from routing.models import FuelStation

# Canadian province/territory codes in the fuel CSV (state column).
CANADIAN_PROVINCES = {
    'AB': 'Alberta',
    'BC': 'British Columbia',
    'MB': 'Manitoba',
    'NB': 'New Brunswick',
    'NL': 'Newfoundland and Labrador',
    'NS': 'Nova Scotia',
    'NT': 'Northwest Territories',
    'NU': 'Nunavut',
    'ON': 'Ontario',
    'PE': 'Prince Edward Island',
    'QC': 'Quebec',
    'SK': 'Saskatchewan',
    'YT': 'Yukon',
}


class Command(BaseCommand):
    help = 'Load fuel station data from CSV file (upserts; does not wipe existing rows)'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='Path to CSV file')
        parser.add_argument('--limit', type=int, default=None, help='Max stations to load')
        parser.add_argument(
            '--only-missing',
            action='store_true',
            help='Only process CSV rows not yet in the database (retry previously skipped rows)',
        )

    def city_name_variants(self, city, state):
        """Build city spellings to try — the CSV often drops apostrophes or splits Mc names."""
        city = ' '.join(city.split())
        state = state.strip().upper()
        variants = []

        def add(name):
            key = name.lower()
            if key not in {v.lower() for v in variants}:
                variants.append(name)

        # Known CSV quirks (city, state) -> corrected name
        aliases = {
            ('odonnell', 'TX'): "O'Donnell",
            ('mc dermitt', 'NV'): 'McDermitt',
        }
        alias = aliases.get((city.lower(), state))
        if alias:
            add(alias)

        add(city)

        # MC DERMITT / Mc Dermitt -> McDermitt
        mc_match = re.match(r'^(?:MC|Mc)\s+([A-Za-z]+)$', city, re.I)
        if mc_match:
            add('Mc' + mc_match.group(1).capitalize())

        # Odonnell -> O'Donnell
        if re.fullmatch(r"O'?Donnell", city, re.I):
            add("O'Donnell")

        # ALL CAPS -> Title Case (e.g. COOKSTOWN stays distinct from above rules)
        if city.isupper() and len(city) > 3:
            add(city.title())

        return variants

    def _nominatim_search(self, query, country_code):
        url = "https://nominatim.openstreetmap.org/search"
        headers = {'User-Agent': 'FuelRouteOptimizer/1.0'}
        params = {
            'q': query,
            'format': 'json',
            'limit': 1,
            'countrycodes': country_code,
        }
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
        return None, None

    def geocode_city_state(self, city, state, address=None):
        """Geocode a city using Nominatim with the correct country context."""
        state = state.strip().upper()
        country = 'ca' if state in CANADIAN_PROVINCES else 'us'
        province = CANADIAN_PROVINCES.get(state, state)
        country_name = 'Canada' if country == 'ca' else 'USA'

        queries = []
        for variant in self.city_name_variants(city, state):
            queries.append(f"{variant}, {state}, {country_name}")
            if state in CANADIAN_PROVINCES:
                queries.append(f"{variant}, {province}, {country_name}")
            if address:
                queries.append(f"{address}, {variant}, {state}, {country_name}")

        seen = set()
        for location in queries:
            if location in seen:
                continue
            seen.add(location)
            try:
                coords = self._nominatim_search(location, country)
                if coords:
                    return coords
            except Exception:
                continue
        return None, None

    def handle(self, *args, **options):
        csv_file = options['csv_file']
        limit = options.get('limit')
        only_missing = options.get('only_missing')

        self.stdout.write(f'Loading fuel data from {csv_file}...')
        if only_missing:
            self.stdout.write(self.style.WARNING('Mode: only-missing (skipping rows already in DB)'))

        created = 0
        updated = 0
        skipped = 0
        unchanged = 0
        geocode_cache = {}
        row_num = 0
        processed = 0

        with open(csv_file, 'r') as file:
            reader = csv.DictReader(file)

            for row in reader:
                row_num += 1

                if limit and processed >= limit:
                    break

                try:
                    station_id_base = row['OPIS Truckstop ID'].strip()
                    name = row['Truckstop Name'].strip()
                    address = row['Address'].strip()
                    city = row['City'].strip()
                    state = row['State'].strip()
                    price_str = row['Retail Price'].strip()

                    if not all([station_id_base, name, city, state, price_str]):
                        skipped += 1
                        continue

                    station_id = f"{station_id_base}-{row_num}"

                    try:
                        price = float(price_str)
                    except ValueError:
                        skipped += 1
                        continue

                    existing = FuelStation.objects.filter(station_id=station_id).first()
                    if only_missing and existing:
                        unchanged += 1
                        continue

                    processed += 1

                    if existing:
                        lat, lon = existing.latitude, existing.longitude
                    else:
                        cache_key = f"{city},{state}"
                        if cache_key in geocode_cache:
                            lat, lon = geocode_cache[cache_key]
                        else:
                            self.stdout.write(f'Geocoding: {city}, {state}')
                            lat, lon = self.geocode_city_state(city, state, address=address)
                            geocode_cache[cache_key] = (lat, lon)
                            time.sleep(1)

                        if lat is None:
                            self.stdout.write(
                                self.style.WARNING(f'Could not geocode: {city}, {state}')
                            )
                            skipped += 1
                            continue

                    defaults = {
                        'name': name,
                        'address': address or 'N/A',
                        'city': city,
                        'state': state,
                        'zip_code': '00000',
                        'latitude': lat,
                        'longitude': lon,
                        'price_per_gallon': price,
                    }

                    _, was_created = FuelStation.objects.update_or_create(
                        station_id=station_id,
                        defaults=defaults,
                    )

                    if was_created:
                        created += 1
                    else:
                        updated += 1

                    if (created + updated) % 50 == 0:
                        self.stdout.write(
                            self.style.SUCCESS(f'Processed {created + updated} stations...')
                        )

                except KeyError as e:
                    self.stdout.write(self.style.ERROR(f'Missing column: {e}'))
                    return
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f'Error on row {row_num}: {e}'))
                    skipped += 1

        self.stdout.write(self.style.SUCCESS(f'\nCreated {created} new stations'))
        self.stdout.write(self.style.SUCCESS(f'Updated {updated} existing stations'))
        if only_missing and unchanged:
            self.stdout.write(f'Left unchanged (already in DB): {unchanged}')
        if skipped > 0:
            self.stdout.write(self.style.WARNING(f'Skipped {skipped} stations'))
