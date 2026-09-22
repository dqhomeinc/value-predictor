import base64
from types import SimpleNamespace

import pytest

from models import BuildLimitsLookup, db
from services import build_limits_search
from services.build_limits_search import (
    _Evidence,
    latest_lookup,
    limits_key,
    lookup_standards,
    verify_limits,
)

CODE_URL = 'https://ecode360.com/12345678'
TABLE = ('Table 2. Schedule of Dimensional Controls\n'
         'District  RS  RO\nMinimum front yard (feet)  30  30\nMinimum side yard (feet)  15  15\n'
         'Maximum height: 2½ stories or 40 feet, whichever is less.\n'
         'Minimum lot area (square feet)  15,500  30,000\n')


def fetched(url, text):
    return SimpleNamespace(type='web_fetch_tool_result', tool_use_id='srvtoolu_1', content=SimpleNamespace(
        type='web_fetch_result', url=url,
        content=SimpleNamespace(type='document', source=SimpleNamespace(type='text', media_type='text/plain',
                                                                         data=text))))


def limit(key, value, unit, quote, url=CODE_URL, section='Table 2'):
    return {'key': key, 'value': value, 'unit': unit, 'quote': quote, 'source_url': url, 'section': section}


def evidence_with(url=CODE_URL, text=TABLE):
    evidence = _Evidence()
    evidence.add([fetched(url, text)])
    return evidence


class TestVerification:
    def test_keeps_a_number_quoted_from_the_fetched_page(self):
        kept, dropped = verify_limits([limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30')],
                                      evidence_with())

        assert kept == [{'key': 'front_setback', 'value': 30, 'unit': 'ft',
                         'quote': 'Minimum front yard (feet) 30 30', 'source_url': CODE_URL, 'section': 'Table 2'}]
        assert dropped == []

    def test_drops_a_quote_that_is_not_on_the_page(self):
        # Claude writes the quote, so a quote that merely contains the
        # number proves nothing until it's found on the page.
        kept, dropped = verify_limits([limit('rear_setback', 15, 'ft', 'Minimum rear yard (feet) 15')],
                                      evidence_with())

        assert kept == []
        assert dropped == [{'key': 'rear_setback', 'value': 15, 'reason': 'quote not found on the page it cites'}]

    def test_drops_a_number_its_quote_does_not_contain(self):
        kept, dropped = verify_limits([limit('side_setback', 12, 'ft', 'Minimum side yard (feet)  15  15')],
                                      evidence_with())

        assert kept == []
        assert dropped[0]['reason'] == "quote doesn't contain the number"

    def test_drops_a_quote_from_a_different_page(self):
        kept, _ = verify_limits([limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30',
                                       url='https://example.com/other')], evidence_with())

        assert kept == []

    def test_tolerates_line_breaks_case_curly_quotes_and_url_variants(self):
        evidence = evidence_with(text='Minimum lot area:\n15,500 “square feet”')
        quote = 'minimum LOT area: 15,500 "square feet"'

        kept, _ = verify_limits([limit('min_lot_area', 15500, 'sq ft', quote, url='http://www.ecode360.com/12345678/')],
                                evidence)

        assert kept[0]['value'] == 15500

    @pytest.mark.parametrize('quote, value, ok', [
        ('Maximum height: 2½ stories or 40 feet', 2.5, True),
        ('Maximum height: 2 1/2 stories or 40 feet', 2.5, True),
        ('Minimum lot area 15,500 square feet', 15500, True),
        ('Maximum floor area ratio of .35 applies', 0.35, True),
        ('Minimum front yard (feet)  130', 30, False),
        ('Minimum front yard (feet)  30.5', 30, False),
        ('Minimum side yard (feet)  30  15  20', 15, True),
    ])
    def test_number_matching(self, quote, value, ok):
        evidence = evidence_with(text=quote)

        kept, _ = verify_limits([limit('front_setback', value, 'ft', quote)], evidence)

        assert bool(kept) is ok

    @pytest.mark.parametrize('item, reason', [
        (limit('garage_size', 30, 'ft', 'Minimum front yard (feet)  30  30'), 'unknown limit'),
        (limit('front_setback', 30, 'meters', 'Minimum front yard (feet)  30  30'), 'unknown unit'),
        (limit('front_setback', 0, 'ft', 'Minimum front yard (feet)  30  30'), 'no usable number'),
        (limit('front_setback', True, 'ft', 'Minimum front yard (feet)  30  30'), 'no usable number'),
        (limit('front_setback', 30, 'ft', '30 feet'), 'quote too short or too long'),
        (limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30', url='javascript:alert(1)'),
         'no source link'),
    ])
    def test_malformed_limits_are_dropped(self, item, reason):
        kept, dropped = verify_limits([item], evidence_with())

        assert kept == []
        assert dropped[0]['reason'] == reason

    def test_a_limit_reported_twice_keeps_the_first(self):
        quote = 'Minimum front yard (feet)  30  30'

        kept, dropped = verify_limits([limit('front_setback', 30, 'ft', quote), limit('front_setback', 30, 'ft', quote)],
                                      evidence_with())

        assert len(kept) == 1
        assert dropped[0]['reason'] == 'reported twice'

    def test_search_excerpts_count_as_evidence(self):
        evidence = _Evidence()
        evidence.add([SimpleNamespace(type='text', text='The bylaw says...', citations=[SimpleNamespace(
            type='web_search_result_location', url=CODE_URL, title='Bylaw',
            cited_text='Minimum front yard (feet) 30 30')])])

        kept, _ = verify_limits([limit('front_setback', 30, 'ft', 'Minimum front yard (feet) 30 30')], evidence)

        assert len(kept) == 1

    def test_fetched_pdfs_are_read(self, monkeypatch):
        monkeypatch.setattr(build_limits_search, '_pdf_text', lambda data: TABLE if base64.b64decode(data) == b'pdf' else '')
        evidence = _Evidence()
        evidence.add([SimpleNamespace(type='web_fetch_tool_result', content=SimpleNamespace(
            type='web_fetch_result', url=CODE_URL, content=SimpleNamespace(type='document', source=SimpleNamespace(
                type='base64', media_type='application/pdf', data=base64.b64encode(b'pdf').decode()))))])

        assert evidence.contains(CODE_URL, 'Minimum front yard (feet)  30  30')

    def test_unreadable_pdf_is_no_evidence(self):
        assert build_limits_search._pdf_text(base64.b64encode(b'not a pdf').decode()) == ''


def analysis(**overrides):
    fields = {'address': '123 Main St, Pittsburgh, PA 15217', 'property_zoning': None,
              'zoning_detail': {'zoning_code': 'RM-M', 'jurisdiction': 'Pittsburgh, PA'}}
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestKey:
    def test_from_discovered_zoning(self):
        assert limits_key(analysis()) == ('Pittsburgh, PA', 'RM-M')

    def test_label_without_a_state_falls_back_to_the_address(self):
        detail = {'zoning_code': 'sf-3', 'jurisdiction': 'AUSTIN FULL PURPOSE'}

        assert limits_key(analysis(address='4100 Avenue G, austin, TX 78751', zoning_detail=detail)) == ('Austin, TX', 'SF-3')

    def test_rentcast_zoning_when_there_is_no_detail(self):
        assert limits_key(analysis(zoning_detail=None, property_zoning='R-1')) == ('Pittsburgh, PA', 'R-1')

    @pytest.mark.parametrize('overrides', [
        {'zoning_detail': None},
        {'zoning_detail': {'zoning_code': ''}, 'address': '123 Main St'},
        {'zoning_detail': {'zoning_code': 'R-1; DROP TABLE'}},
        {'zoning_detail': {'zoning_code': 'x' * 40}},
    ])
    def test_none_without_a_usable_district_and_place(self, overrides):
        assert limits_key(analysis(**overrides)) is None


def saved(**overrides):
    fields = {'jurisdiction': 'Pittsburgh, PA', 'district': 'RM-M', 'status': 'found',
              'code_title': 'Pittsburgh Zoning Code', 'code_url': CODE_URL}
    fields.update(overrides)
    return BuildLimitsLookup(**fields)


def stored(key, value, unit, quote, section='Table 4.1'):
    return {'key': key, 'value': value, 'unit': unit, 'quote': quote, 'source_url': CODE_URL, 'section': section}


class TestSavedRows:
    def test_nothing_saved_yet(self, app):
        assert latest_lookup('Pittsburgh, PA', 'RM-M') is None

    def test_newest_finished_search_wins_and_failures_never_show(self, app):
        db.session.add_all([saved(status='not_found'), saved(limits=[]), saved(status='failed')])
        db.session.commit()

        assert latest_lookup('Pittsburgh, PA', 'RM-M').status == 'found'

    def test_a_found_row_becomes_the_what_you_can_build_table(self, app):
        row = saved(
            limits=[stored('min_lot_area', 10000, 'sq ft', 'Minimum lot area: 10,000 square feet'),
                    stored('front_setback', 25, 'ft', 'Minimum front yard: 25 feet'),
                    stored('max_lot_coverage', 30, '%', 'Maximum lot coverage: 30 percent')],
            dropped=[{'key': 'max_stories', 'value': 3, 'reason': 'quote not found on the page it cites'}])
        db.session.add(row)
        db.session.commit()

        standards = lookup_standards(row)

        assert standards['kind'] == 'ai_search'
        # Display order, not the order they were reported in.
        assert [rule['label'] for rule in standards['rules']] == [
            'Front setback', 'Maximum lot coverage', 'Minimum lot']
        rules = {rule['label']: rule for rule in standards['rules']}
        assert rules['Front setback']['value'] == '25 ft'
        assert rules['Maximum lot coverage']['value'] == '30%'
        assert rules['Minimum lot']['value'] == '10,000 sq ft'
        assert rules['Front setback']['note'] == '"Minimum front yard: 25 feet" (Table 4.1)'
        assert rules['Front setback']['source_url'] == CODE_URL
        assert standards['source']['citation'] == 'Pittsburgh Zoning Code'
        assert standards['source']['url'] == CODE_URL

    def test_says_how_many_values_were_dropped(self, app):
        one = saved(limits=[stored('front_setback', 25, 'ft', 'Minimum front yard: 25 feet')],
                    dropped=[{'key': 'max_stories', 'value': 3, 'reason': 'x'}])
        two = saved(limits=list(one.limits), dropped=[{'key': 'a', 'value': 1, 'reason': 'x'},
                                                      {'key': 'b', 'value': 2, 'reason': 'x'}])

        assert "1 other value it reported didn't match its source and isn't shown." in lookup_standards(one)['caveat']
        assert "2 other values it reported didn't match their sources and aren't shown." in lookup_standards(two)['caveat']
        assert 'other value' not in lookup_standards(saved(limits=list(one.limits)))['caveat']

    def test_unrecognized_keys_are_left_out(self, app):
        # A stored row can outlive a rename of the limits it holds.
        row = saved(limits=[stored('parking_spaces', 2, 'ft', 'Two parking spaces are required')])

        assert lookup_standards(row)['rules'] == []

    def test_a_row_without_a_code_title_still_names_a_source(self, app):
        assert lookup_standards(saved(code_title=''))['source']['citation'] == "Pittsburgh, PA's zoning code"
