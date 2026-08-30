"""
Manual smoke test for the RentCast integration (Phase 2 verification).

Spends real RentCast API calls: 2 for an address not already cached, 0 for
a repeat (property_lookup_cache never expires).

Usage:
    python scripts/smoke_test_rentcast.py "5500 Grand Lake Dr, San Antonio, TX 78244"
"""
import os
import sys

# Running this as `python scripts/smoke_test_rentcast.py` only puts scripts/
# on sys.path, not the repo root where app.py/integrations/ live — add it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from integrations.rentcast import RentCastError, RentCastClient  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/smoke_test_rentcast.py "<address>"')
        sys.exit(1)
    address = sys.argv[1]

    api_key = os.environ.get('RENTCAST_API_KEY')
    if not api_key:
        print('RENTCAST_API_KEY is not set — check your .env file.')
        sys.exit(1)

    app = create_app()
    with app.app_context():
        client = RentCastClient(api_key=api_key)
        try:
            avm_json, property_json, from_cache, source = client.lookup_property(address)
        except RentCastError as exc:
            print(f'RentCast lookup failed: {exc}')
            sys.exit(1)

        subject = avm_json.get('subjectProperty', {})
        price = avm_json.get('price')
        price_low = avm_json.get('priceRangeLow')
        price_high = avm_json.get('priceRangeHigh')
        comps = avm_json.get('comparables', [])

        print(f'Address:        {address}')
        print(f"From cache:     {from_cache} (source={source})  ({'0' if from_cache else '2'} API calls spent)")
        print(f'Market value:   {f"${price:,}" if price is not None else "(unavailable)"}')
        if price_low is not None and price_high is not None:
            print(f'Price range:    ${price_low:,} - ${price_high:,}')
        print(f'Comps count:    {len(comps)}')
        print(f'Sqft:           {subject.get("squareFootage")}')
        print(f'Lot size:       {subject.get("lotSize")}')
        print(f'Bed/Bath:       {subject.get("bedrooms")} / {subject.get("bathrooms")}')
        print(f'Year built:     {subject.get("yearBuilt")}')
        print(f'Zoning:         {property_json.get("zoning")}')
        print(f'Subdivision:    {property_json.get("subdivision")}')


if __name__ == '__main__':
    main()
