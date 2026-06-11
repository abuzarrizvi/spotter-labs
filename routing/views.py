import time
from decimal import Decimal
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from drf_spectacular.utils import extend_schema, OpenApiExample
from .serializers import RouteRequestSerializer, RouteResponseSerializer, FuelStationSerializer
from .services import RouteService, FuelOptimizer, MapGenerator


class OptimalRouteView(APIView):
    """Calculate optimal fuel stops for a route"""

    @extend_schema(
        request=RouteRequestSerializer,
        responses={200: RouteResponseSerializer},
        examples=[
            OpenApiExample(
                'Chicago to Los Angeles',
                value={"start": "Chicago, IL", "finish": "Los Angeles, CA"},
                request_only=True,
            ),
            OpenApiExample(
                'New York to Miami',
                value={"start": "New York, NY", "finish": "Miami, FL"},
                request_only=True,
            ),
        ],
        description="""
Calculate the most cost-effective fuel stops along a driving route within the USA.

**Vehicle Specs:**
- Max range: 500 miles
- Fuel consumption: 10 MPG
- Tank capacity: 50 gallons

**Algorithm:**
1. Start with full tank (500-mile range)
2. Travel up to 450 miles (safety margin) - BUT its DP so it checks 50-250 miles for each stop for fuel costs optimization
3. Find cheapest station within search radius of route
4. Refuel to full capacity
5. Repeat until destination

**External API Calls:**
- OSRM: 1 call for routing
- Nominatim: 2 calls for geocoding
        """
    )
    def post(self, request):
        start_time = time.time()
        serializer = RouteRequestSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {"error": "Invalid input", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        start = serializer.validated_data['start']
        finish = serializer.validated_data['finish']

        try:
            route_service = RouteService()
            route_data = route_service.get_route(start, finish)
            route_data['start_location'] = start
            route_data['finish_location'] = finish

            fuel_optimizer = FuelOptimizer(route_data)
            fuel_stops = fuel_optimizer.calculate_optimal_stops()

            serialized_stops = []
            for stop in fuel_stops:
                station_serializer = FuelStationSerializer(stop['station'])
                serialized_stop = {
                    'stop_number': stop['stop_number'],
                    'station': station_serializer.data,
                    'distance_from_start': stop['distance_from_start'],
                    'cumulative_distance': stop['cumulative_distance'],
                    'fuel_amount_gallons': stop['fuel_amount_gallons'],
                    'cost': float(stop['cost'])
                }
                serialized_stops.append(serialized_stop)

            totals = fuel_optimizer.compute_trip_totals(
                fuel_stops,
                getattr(fuel_optimizer, '_last_candidates', None),
            )
            total_fuel_cost = totals['total_fuel_cost']
            total_fuel_gallons = totals['total_fuel_gallons']

            map_generator = MapGenerator()
            map_url = map_generator.generate_map_url(route_data, fuel_stops)
            response_time_ms = int((time.time() - start_time) * 1000)

            response_data = {
                'route': {
                    'distance_miles': round(route_data['distance_miles'], 2),
                    'duration_hours': round(route_data['duration_hours'], 2),
                    'start_location': start,
                    'finish_location': finish,
                    'geometry': route_data['geometry'][:100]
                },
                'fuel_stops': serialized_stops,
                'total_fuel_cost': total_fuel_cost,
                'total_fuel_gallons': total_fuel_gallons,
                'starting_fuel_gallons': totals['starting_fuel_gallons'],
                'starting_fuel_cost': totals['starting_fuel_cost'],
                'total_distance_miles': round(route_data['distance_miles'], 2),
                'response_time_ms': response_time_ms,
                'map_endpoint': '/api/route/map/'
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"error": "Internal server error", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RouteMapView(APIView):
    """Generate interactive HTML map for a route"""

    @extend_schema(
        request=RouteRequestSerializer,
        responses={200: {"type": "string", "format": "html"}},
        description="""
Returns an interactive HTML map showing the route with fuel stops.

**How to view the map:**

**Option 1 - Save to file (Recommended):**
```bash
curl -X POST http://localhost:8000/api/route/map/ \\
  -H "Content-Type: application/json" \\
  -d '{"start":"Chicago, IL","finish":"Los Angeles, CA"}' \\
  -o map.html

# Then open map.html in your browser
```

**Option 2 - Postman:**
1. Make POST request
2. Click "Preview" tab to see the rendered map

**Option 3 - Browser:**
Navigate to http://localhost:8000/api/route/map/ and use the form
        """
    )
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {"error": "Invalid input", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            start = serializer.validated_data['start']
            finish = serializer.validated_data['finish']

            route_service = RouteService()
            route_data = route_service.get_route(start, finish)
            route_data['start_location'] = start
            route_data['finish_location'] = finish

            fuel_optimizer = FuelOptimizer(route_data)
            fuel_stops = fuel_optimizer.calculate_optimal_stops()

            map_generator = MapGenerator()
            map_html = map_generator.generate_map_html(route_data, fuel_stops)

            return HttpResponse(map_html, content_type='text/html')

        except Exception as e:
            return HttpResponse(
                f"<html><body><h1>Error generating map</h1><p>{str(e)}</p></body></html>",
                status=500
            )


class HealthCheckView(APIView):
    """Health check endpoint"""

    @extend_schema(
        description="Verify API is running and healthy",
        responses={
            200: {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "example": "healthy"},
                    "service": {"type": "string", "example": "Fuel Route Optimizer API"},
                    "version": {"type": "string", "example": "1.0.0"}
                }
            }
        }
    )
    def get(self, request):
        return Response({
            "status": "healthy",
            "service": "Fuel Route Optimizer API",
            "version": "1.0.0"
        })