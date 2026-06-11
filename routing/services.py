import requests
import math
from typing import List, Dict, Tuple, Optional
from decimal import Decimal
from django.conf import settings
from django.core.cache import cache
from .models import FuelStation


class RouteService:
    """Handles external routing API calls"""

    def get_route(self, start: str, finish: str) -> Dict:
        cache_key = f"route:{start}:{finish}".replace(" ", "_").replace(",", "")
        cached_route = cache.get(cache_key)
        if cached_route:
            return cached_route

        start_coords = self._geocode(start)
        finish_coords = self._geocode(finish)
        route_data = self._get_route_osrm(start_coords, finish_coords)

        cache.set(cache_key, route_data, 3600)
        return route_data

    def _geocode(self, location: str) -> Tuple[float, float]:
        cache_key = f"geocode:{location}".replace(" ", "_").replace(",", "")
        cached_coords = cache.get(cache_key)
        if cached_coords:
            return cached_coords

        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': location,
            'format': 'json',
            'limit': 1,
            'countrycodes': 'us'
        }
        headers = {'User-Agent': 'FuelRouteOptimizer/1.0'}

        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not data:
            raise ValueError(f"Could not find location: {location}")

        coords = (float(data[0]['lon']), float(data[0]['lat']))
        cache.set(cache_key, coords, 86400)
        return coords

    def _get_route_osrm(self, start: Tuple[float, float], finish: Tuple[float, float]) -> Dict:
        url = f"http://router.project-osrm.org/route/v1/driving/{start[0]},{start[1]};{finish[0]},{finish[1]}"
        params = {'overview': 'full', 'geometries': 'geojson', 'steps': 'true'}

        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        if data['code'] != 'Ok':
            raise ValueError("Routing failed")

        route = data['routes'][0]
        distance_meters = route['distance']
        duration_seconds = route['duration']
        geometry = route['geometry']['coordinates']
        waypoints = self._extract_waypoints(geometry, distance_meters)

        return {
            'distance_miles': distance_meters * 0.000621371,
            'duration_hours': duration_seconds / 3600,
            'geometry': geometry,
            'waypoints': waypoints,
            'start_coords': start,
            'finish_coords': finish
        }

    def _extract_waypoints(self, geometry: List, total_distance_meters: float) -> List[Tuple]:
        waypoints = []
        interval_miles = 50
        interval_meters = interval_miles * 1609.34
        cumulative_distance = 0
        waypoints.append((geometry[0][0], geometry[0][1], 0))

        for i in range(1, len(geometry)):
            prev_point = geometry[i-1]
            curr_point = geometry[i]
            segment_distance = self._haversine_distance(
                prev_point[1], prev_point[0], curr_point[1], curr_point[0]
            )
            cumulative_distance += segment_distance

            if cumulative_distance >= interval_meters * (len(waypoints)):
                waypoints.append((
                    curr_point[0], curr_point[1],
                    cumulative_distance * 0.000621371
                ))

        if waypoints[-1][2] < total_distance_meters * 0.000621371 - 10:
            waypoints.append((
                geometry[-1][0], geometry[-1][1],
                total_distance_meters * 0.000621371
            ))

        return waypoints

    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = math.sin(delta_phi/2)**2 + \
            math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c


class FuelOptimizer:
    """Plans cost-effective fuel stops along a route.

    START_WITH_FULL_TANK (settings) controls stop placement only:
      * True  -> virtual full tank at departure; stops appear once ~500 mi range
                 is used.
      * False -> depart empty; first stop must be near the actual start coords.

    Total trip cost (compute_trip_totals) always bills every mile at 10 MPG,
    including fuel consumed before the first refuel stop.
    """

    MAX_RANGE_MILES = 500
    MPG = 10
    TANK_CAPACITY_GALLONS = MAX_RANGE_MILES / MPG  # 50 gal usable
    CORRIDOR_RADIUS_MILES = 50   # preferred max distance a station may sit off-route
    CORRIDOR_RADIUS_EXPANDED = (50, 100, 150, 200, 250)  # widen if route has gaps
    WAYPOINT_INTERVAL_MILES = 10  # resolution for projecting stations onto route
    STOP_PENALTY = 5.0  # planner-only; not added to API dollar totals

    def __init__(self, route_data: Dict):
        self.route_data = route_data
        self.total_distance = route_data['distance_miles']
        self.geometry = route_data['geometry']
        self._start_lon = self.geometry[0][0]
        self._start_lat = self.geometry[0][1]
        self._last_candidates = None

    def _start_fuel_radius(self) -> float:
        return getattr(settings, 'START_FUEL_RADIUS_MILES', 50)

    def _distance_from_start_coords(self, lat: float, lon: float) -> float:
        return self._haversine_miles(self._start_lat, self._start_lon, lat, lon)

    def _start_area_fuel_price(self, candidates: Optional[List[Dict]] = None) -> float:
        """Cheapest $/gal among stations near the route start."""
        radius = self._start_fuel_radius()
        prices = []
        if candidates:
            for c in candidates:
                s = c['station']
                if self._distance_from_start_coords(s.latitude, s.longitude) <= radius:
                    prices.append(c['price'])
        if not prices:
            margin = radius / 69.0
            stations = FuelStation.objects.filter(
                latitude__gte=self._start_lat - margin,
                latitude__lte=self._start_lat + margin,
                longitude__gte=self._start_lon - margin,
                longitude__lte=self._start_lon + margin,
            )
            for s in stations:
                if self._distance_from_start_coords(s.latitude, s.longitude) <= radius:
                    prices.append(float(s.price_per_gallon))
        return min(prices) if prices else 3.50

    def compute_trip_totals(self, fuel_stops: List[Dict],
                            candidates: Optional[List[Dict]] = None) -> Dict:
        """Return total gallons and cost for the entire trip at 10 MPG."""
        d = self.total_distance
        total_gallons = round(d / self.MPG, 2)
        full_tank = getattr(settings, 'START_WITH_FULL_TANK', True)
        start_price = self._start_area_fuel_price(candidates)

        if not fuel_stops:
            return {
                'total_fuel_gallons': total_gallons,
                'total_fuel_cost': round(total_gallons * start_price, 2),
                'starting_fuel_gallons': total_gallons,
                'starting_fuel_cost': round(total_gallons * start_price, 2),
            }

        cost = sum(float(s['cost']) for s in fuel_stops)
        first_mile = fuel_stops[0]['distance_from_start']
        last_mile = fuel_stops[-1]['distance_from_start']
        remaining = max(0.0, d - last_mile)

        starting_gallons = 0.0
        starting_cost = 0.0

        if full_tank and first_mile > 0:
            # Miles covered on the starting tank are billed at start-area price
            # (not included in any stop's purchase line).
            starting_gallons = first_mile / self.MPG
            starting_cost = starting_gallons * start_price
            cost += starting_cost
        elif not full_tank and first_mile > 0:
            # Approach fuel is already rolled into the first stop's cost.
            first_price = float(fuel_stops[0]['station'].price_per_gallon)
            starting_gallons = first_mile / self.MPG
            starting_cost = starting_gallons * first_price

        if remaining > 0.01:
            avg_price = (
                sum(float(s['station'].price_per_gallon) for s in fuel_stops)
                / len(fuel_stops)
            )
            cost += (remaining / self.MPG) * avg_price

        return {
            'total_fuel_gallons': total_gallons,
            'total_fuel_cost': round(cost, 2),
            'starting_fuel_gallons': round(starting_gallons, 2),
            'starting_fuel_cost': round(starting_cost, 2),
        }

    def calculate_optimal_stops(self) -> List[Dict]:
        last_error = None
        for radius in self.CORRIDOR_RADIUS_EXPANDED:
            candidates = self._build_route_candidates(corridor_radius=radius)
            if not candidates:
                continue
            try:
                stops = self._solve_optimal(candidates)
                self._last_candidates = candidates
                return stops
            except ValueError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise ValueError(
            "No fuel stations found near this route. Try loading more "
            "stations or a different route."
        )

    # ------------------------------------------------------------------ #
    # Step 1: find every station near the route corridor and project it   #
    #         onto the route as a distance-from-start (miles).            #
    # ------------------------------------------------------------------ #
    def _build_waypoints(self) -> List[Tuple[float, float, float]]:
        """Return [(lon, lat, cumulative_miles), ...] sampled every ~10 miles."""
        waypoints = [(self.geometry[0][0], self.geometry[0][1], 0.0)]
        cumulative = 0.0
        last_added = 0.0
        for i in range(1, len(self.geometry)):
            prev, curr = self.geometry[i - 1], self.geometry[i]
            seg = self._haversine_miles(prev[1], prev[0], curr[1], curr[0])
            cumulative += seg
            if cumulative - last_added >= self.WAYPOINT_INTERVAL_MILES:
                waypoints.append((curr[0], curr[1], cumulative))
                last_added = cumulative
        waypoints.append((self.geometry[-1][0], self.geometry[-1][1], cumulative))
        return waypoints

    def _build_route_candidates(self, corridor_radius=None) -> List[Dict]:
        corridor_radius = corridor_radius or self.CORRIDOR_RADIUS_MILES
        waypoints = self._build_waypoints()

        lats = [g[1] for g in self.geometry]
        lons = [g[0] for g in self.geometry]
        margin = corridor_radius / 69.0
        stations = FuelStation.objects.filter(
            latitude__gte=min(lats) - margin,
            latitude__lte=max(lats) + margin,
            longitude__gte=min(lons) - margin,
            longitude__lte=max(lons) + margin,
        )

        candidates = []
        for s in stations:
            nearest = None
            best_off = None
            for wlon, wlat, wdist in waypoints:
                off = self._haversine_miles(s.latitude, s.longitude, wlat, wlon)
                if best_off is None or off < best_off:
                    best_off = off
                    nearest = wdist
            if best_off is not None and best_off <= corridor_radius:
                candidates.append({
                    'station': s,
                    'distance': nearest,
                    'price': float(s.price_per_gallon),
                })

        candidates.sort(key=lambda c: c['distance'])
        # Keep only the cheapest station at each distance bucket to shrink the
        # search space without losing optimality.
        deduped = {}
        for c in candidates:
            bucket = round(c['distance'])
            if bucket not in deduped or c['price'] < deduped[bucket]['price']:
                deduped[bucket] = c
        return sorted(deduped.values(), key=lambda c: c['distance'])

    # ------------------------------------------------------------------ #
    # Step 2: cost-minimal refueling with realistic, consolidated stops.  #
    #                                                                      #
    # We model how refueling actually works: at each stop the driver buys #
    # enough fuel to reach the NEXT stop (a leg of at most one tank-range, #
    # R miles). The total cost is the sum over chosen stops of             #
    #     (leg_distance / MPG) * price_at_that_stop.                       #
    # A DP over candidate stations finds the set of stops that minimises   #
    # this cost. Because there is never any benefit to stopping twice in a #
    # stretch where one tank suffices, this naturally consolidates nearby  #
    # similar-priced stations into a single fill-up -- no micro-stops.     #
    # ------------------------------------------------------------------ #
    def _solve_optimal(self, candidates: List[Dict]) -> List[Dict]:
        D = self.total_distance
        R = self.MAX_RANGE_MILES
        EPS = 1e-6
        full_tank = getattr(settings, 'START_WITH_FULL_TANK', True)

        nodes = list(candidates)
        if nodes[0]['distance'] > R:
            raise ValueError(
                f"No fuel station within {R} miles of the start. Cannot plan a route."
            )

        start_radius = self._start_fuel_radius()
        first_stop_indices = set()
        for idx, node in enumerate(nodes):
            s = node['station']
            if self._distance_from_start_coords(s.latitude, s.longitude) <= start_radius:
                first_stop_indices.add(idx)

        if full_tank:
            nodes = [{'station': None, 'distance': 0.0, 'price': 0.0,
                      'virtual': True, 'full_tank': True}] + nodes
        else:
            if not first_stop_indices:
                raise ValueError(
                    f"No fuel station within {start_radius} miles of the start. "
                    f"Cannot depart on an empty tank."
                )
            nodes = [{'station': None, 'distance': 0.0, 'price': 0.0,
                      'virtual': True, 'empty': True}] + nodes
            first_stop_indices = {i + 1 for i in first_stop_indices}

        n = len(nodes)
        INF = float('inf')
        dp = [INF] * n
        nxt = [-1] * n
        leg_to = [0.0] * n
        approach = [0.0] * n  # extra miles before first purchase at this stop

        order = sorted(range(n), key=lambda i: nodes[i]['distance'], reverse=True)
        for i in order:
            if nodes[i].get('virtual'):
                continue
            di, pi = nodes[i]['distance'], nodes[i]['price']
            penalty = self.STOP_PENALTY
            if D - di <= R + EPS:
                dp[i] = (D - di) / self.MPG * pi + penalty
                leg_to[i] = D - di
                nxt[i] = -1
            for j in range(n):
                if nodes[j].get('virtual') or nodes[j]['distance'] <= di + EPS:
                    continue
                leg = nodes[j]['distance'] - di
                if leg > R + EPS or dp[j] == INF:
                    continue
                cost = leg / self.MPG * pi + penalty + dp[j]
                if cost < dp[i] - EPS:
                    dp[i] = cost
                    nxt[i] = j
                    leg_to[i] = leg

        # Virtual origin (index 0): connect to first real stop(s).
        if full_tank:
            for j in range(1, n):
                dj = nodes[j]['distance']
                if dj > R + EPS or dp[j] == INF:
                    continue
                cost = dp[j] + self.STOP_PENALTY
                if cost < dp[0] - EPS:
                    dp[0] = cost
                    nxt[0] = j
                    leg_to[0] = dj
                    approach[j] = 0.0
        else:
            for j in first_stop_indices:
                dj = nodes[j]['distance']
                if dj > R + EPS or dp[j] == INF:
                    continue
                pj = nodes[j]['price']
                cost = dj / self.MPG * pj + self.STOP_PENALTY + dp[j]
                if cost < dp[0] - EPS:
                    dp[0] = cost
                    nxt[0] = j
                    leg_to[0] = dj
                    approach[j] = dj

        if dp[0] == INF:
            raise ValueError(
                f"Range gap: stations are more than {R} miles apart along this "
                f"route. Load more stations to plan it."
            )

        stops = []
        i = nxt[0]
        stop_number = 1
        while i is not None and i != -1:
            if nodes[i].get('virtual'):
                i = nxt[i]
                continue
            leg = leg_to[i]
            extra = approach[i]
            gallons = (leg + extra) / self.MPG
            if gallons > EPS:
                price = nodes[i]['station'].price_per_gallon
                stops.append({
                    'stop_number': stop_number,
                    'station': nodes[i]['station'],
                    'distance_from_start': nodes[i]['distance'],
                    'cumulative_distance': nodes[i]['distance'],
                    'fuel_amount_gallons': round(gallons, 2),
                    'cost': Decimal(str(round(gallons, 4))) * price,
                })
                stop_number += 1
                approach[i] = 0.0
            i = nxt[i]
        return stops

    @staticmethod
    def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 3959  # earth radius in miles
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class MapGenerator:
    """Generates map visualization"""

    @staticmethod
    def generate_map_html(route_data: Dict, fuel_stops: List[Dict]) -> str:
        """Generate interactive HTML map with route and fuel stops"""
        try:
            import folium
        except ImportError:
            return "<p>Map generation requires folium library. Install with: pip install folium</p>"

        start = route_data['start_coords']
        finish = route_data['finish_coords']
        center_lat = (start[1] + finish[1]) / 2
        center_lon = (start[0] + finish[0]) / 2

        distance = route_data['distance_miles']
        zoom = 5 if distance > 1000 else (6 if distance > 500 else 7)

        m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom)

        # Start marker (green)
        folium.Marker(
            [start[1], start[0]],
            popup=f"<b>Start:</b> {route_data.get('start_location', 'Start')}",
            icon=folium.Icon(color='green', icon='play')
        ).add_to(m)

        # Finish marker (red)
        folium.Marker(
            [finish[1], finish[0]],
            popup=f"<b>Finish:</b> {route_data.get('finish_location', 'Finish')}",
            icon=folium.Icon(color='red', icon='stop')
        ).add_to(m)

        # Route line (blue)
        route_coords = [[coord[1], coord[0]] for coord in route_data['geometry']]
        folium.PolyLine(
            route_coords,
            color='blue',
            weight=4,
            opacity=0.7,
            popup=f"Route: {distance:.1f} miles"
        ).add_to(m)

        # Fuel stops (orange markers)
        for stop in fuel_stops:
            station = stop['station']
            # Handle both dict and object
            if isinstance(station, dict):
                lat = station['latitude']
                lon = station['longitude']
                name = station['name']
                city = station['city']
                state = station['state']
                price = station['price_per_gallon']
            else:
                lat = station.latitude
                lon = station.longitude
                name = station.name
                city = station.city
                state = station.state
                price = station.price_per_gallon

            folium.Marker(
                [lat, lon],
                popup=f"""
                <div style='width: 200px'>
                    <b>Stop #{stop['stop_number']}</b><br>
                    <b>{name}</b><br>
                    {city}, {state}<br>
                    <hr>
                    <b>Price:</b> ${price}/gal<br>
                    <b>Distance:</b> {stop['distance_from_start']:.1f} mi<br>
                    <b>Fuel:</b> {stop['fuel_amount_gallons']:.1f} gal<br>
                    <b>Cost:</b> ${float(stop['cost']):.2f}
                </div>
                """,
                icon=folium.Icon(color='orange', icon='info-sign')
            ).add_to(m)

        return m._repr_html_()

    @staticmethod
    def generate_map_url(route_data: Dict, fuel_stops: List[Dict]) -> str:
        """Generate OpenStreetMap URL"""
        start = route_data['start_coords']
        finish = route_data['finish_coords']
        center_lat = (start[1] + finish[1]) / 2
        center_lon = (start[0] + finish[0]) / 2

        distance = route_data['distance_miles']
        zoom = 5 if distance > 1000 else (6 if distance > 500 else 7)

        return f"https://www.openstreetmap.org/?mlat={center_lat}&mlon={center_lon}#map={zoom}/{center_lat}/{center_lon}"