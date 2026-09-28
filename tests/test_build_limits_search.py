import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import pytest

from models import BuildLimitsLookup, User, db
from services import build_limits_search
from services.build_limits_search import (
    RECORD_TOOL_NAME,
    LimitsSearchError,
    MockLimitsClient,
    _Evidence,
    _url_key,
    build_limits_client,
    daily_search_limit,
    latest_lookup,
    limits_configured,
    limits_key,
    limits_mock_enabled,
    limits_model,
    lookup_standards,
    run_lookup,
    search_build_limits,
    searches_in_last_day,
    verify_limits,
)

CODE_URL = 'https://ecode360.com/12345678'
TABLE = ('Table 2. Schedule of Dimensional Controls\n'
         'District  RS  RO\nMinimum front yard (feet)  30  30\nMinimum side yard (feet)  15  15\n'
         'Maximum height: 2½ stories or 40 feet, whichever is less.\n'
         'Minimum lot area (square feet)  15,500  30,000\n')


class FakeAPIError(anthropic.APIError):
    """An API failure without the HTTP request the SDK's own constructors need."""

    def __init__(self):
        self.message = 'Claude is unavailable'
        Exception.__init__(self, self.message)


def fetched(url, text):
    return SimpleNamespace(type='web_fetch_tool_result', tool_use_id='srvtoolu_1', content=SimpleNamespace(
        type='web_fetch_result', url=url,
        content=SimpleNamespace(type='document', source=SimpleNamespace(type='text', media_type='text/plain',
                                                                         data=text))))


def recorded(limits, found=True, **fields):
    payload = {'found': found, 'code_title': 'Zoning Bylaw', 'code_url': CODE_URL, 'district_as_written': 'RS',
               'notes': '', 'limits': limits, **fields}
    return SimpleNamespace(type='tool_use', id='toolu_1', name=RECORD_TOOL_NAME, input=payload)


def limit(key, value, unit, quote, url=CODE_URL, section='Table 2'):
    return {'key': key, 'value': value, 'unit': unit, 'quote': quote, 'source_url': url, 'section': section}


def response(*content, stop_reason='end_turn', searches=0, fetches=0):
    return SimpleNamespace(content=list(content), stop_reason=stop_reason, usage=SimpleNamespace(
        server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=fetches)))


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


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

    @pytest.mark.parametrize('key, unit, quote, value, ok', [
        ('max_stories', 'stories', 'Maximum height: 2½ stories or 40 feet', 2.5, True),
        ('max_stories', 'stories', 'Maximum height: 2 1/2 stories or 40 feet', 2.5, True),
        ('min_lot_area', 'sq ft', 'Minimum lot area 15,500 square feet', 15500, True),
        ('max_floor_area_ratio', 'ratio', 'Maximum floor area ratio of .35 applies', 0.35, True),
        ('front_setback', 'ft', 'Minimum front yard (feet)  130', 30, False),
        ('front_setback', 'ft', 'Minimum front yard (feet)  30.5', 30, False),
        ('side_setback', 'ft', 'Minimum side yard (feet)  30  15  20', 15, True),
    ])
    def test_number_matching(self, key, unit, quote, value, ok):
        evidence = evidence_with(text=quote)

        kept, _ = verify_limits([limit(key, value, unit, quote)], evidence)

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

    @pytest.mark.parametrize('key, unit, quote', [
        # Right number, wrong row: a coverage percentage filed as a height in feet.
        ('max_height', 'ft', 'Maximum lot coverage (percent)  35  25'),
        ('max_lot_coverage', '%', 'Minimum front yard (feet)  35  25'),
        ('min_lot_area', 'sq ft', 'Minimum lot area (acres)  35'),
    ])
    def test_a_quote_in_other_units_is_dropped(self, key, unit, quote):
        kept, dropped = verify_limits([limit(key, 35, unit, quote)], evidence_with(text=quote))

        assert kept == []
        assert dropped[0]['reason'] == 'quote names different units'

    def test_a_quote_naming_both_units_serves_either(self):
        quote = 'Maximum height: 2 1/2 stories or 40 feet, whichever is less.'
        evidence = evidence_with(text=quote)

        for item in (limit('max_height', 40, 'ft', quote), limit('max_stories', 2.5, 'stories', quote)):
            assert verify_limits([item], evidence)[0], item['key']

    def test_a_quote_naming_no_unit_is_left_alone(self):
        # Plenty of real table rows carry the unit in a column header instead.
        quote = 'Minimum front yard  30  30  25'

        assert verify_limits([limit('front_setback', 30, 'ft', quote)], evidence_with(text=quote))[0]

    def test_a_redirected_fetch_still_backs_the_url_that_was_cited(self):
        # Town code sites redirect; the quote cites the url Claude asked for,
        # not the one the fetch landed on.
        evidence = _Evidence()
        evidence.add([
            SimpleNamespace(type='server_tool_use', id='srvtoolu_1', name='web_fetch',
                            input={'url': CODE_URL}),
            fetched('https://ecode360.com/12345678/laws/NEW-PATH', TABLE),
        ])

        kept, _ = verify_limits([limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30')], evidence)

        assert len(kept) == 1

    def test_the_url_a_fetch_landed_on_still_counts(self):
        evidence = _Evidence()
        evidence.add([
            SimpleNamespace(type='server_tool_use', id='srvtoolu_1', name='web_fetch',
                            input={'url': 'https://ecode360.com/old'}),
            fetched(CODE_URL, TABLE),
        ])

        kept, _ = verify_limits([limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30')], evidence)

        assert len(kept) == 1

    def test_a_fetch_that_did_not_redirect_is_stored_once(self):
        evidence = _Evidence()
        evidence.add([
            SimpleNamespace(type='server_tool_use', id='srvtoolu_1', name='web_fetch',
                            input={'url': CODE_URL}),
            fetched(CODE_URL, TABLE),
        ])

        assert [len(texts) for texts in evidence.pages.values()] == [1]

    @pytest.mark.parametrize('key', [['front_setback'], {'key': 'front_setback'}, None, 7])
    def test_a_key_that_is_not_a_limit_name_is_dropped(self, key):
        # An unhashable key would raise on the LIMITS lookup rather than be
        # dropped the way every other malformed field is.
        kept, dropped = verify_limits([limit(key, 30, 'ft', 'Minimum front yard (feet)  30  30')],
                                      evidence_with())

        assert kept == []
        assert dropped[0]['reason'] == 'unknown limit'

    def test_only_a_web_fetch_supplies_the_url_it_asked_for(self):
        # Pairing is by tool_use_id; requiring the name keeps that from
        # resting on ids being unique across the different server tools.
        evidence = _Evidence()
        evidence.add([
            SimpleNamespace(type='server_tool_use', id='srvtoolu_1', name='web_search',
                            input={'url': 'https://example.com/not-fetched'}),
            fetched(CODE_URL, TABLE),
        ])

        assert list(evidence.pages) == [_url_key(CODE_URL)]

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


class TestSearch:
    def test_finds_checks_and_counts(self):
        client = FakeClient(response(
            fetched(CODE_URL, TABLE),
            recorded([limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30'),
                      limit('rear_setback', 15, 'ft', 'Minimum rear yard (feet) 15')]),
            stop_reason='tool_use', searches=2, fetches=1))

        result = search_build_limits(client, 'Lexington, MA', 'RS')

        assert result.found is True
        assert [item['key'] for item in result.limits] == ['front_setback']
        assert [item['key'] for item in result.dropped] == ['rear_setback']
        assert (result.code_title, result.code_url, result.district_as_written) == ('Zoning Bylaw', CODE_URL, 'RS')
        assert (result.web_searches, result.web_fetches) == (2, 1)

    def test_request_carries_the_task_and_tools(self):
        client = FakeClient(response(recorded([], found=False), stop_reason='tool_use'))

        search_build_limits(client, 'Lexington, MA', 'RS')

        call = client.calls[0]
        assert call['model'] == limits_model()
        assert 'zoning code of Lexington, MA sets for the "RS" zoning district' in call['system']
        assert call['messages'] == [{'role': 'user', 'content': 'Find the build limits for the "RS" zoning district in Lexington, MA.'}]
        search, fetch, record = call['tools']
        assert search['type'] == 'web_search_20250305'
        assert search['user_location'] == {'type': 'approximate', 'city': 'Lexington', 'region': 'MA', 'country': 'US'}
        assert fetch['type'] == 'web_fetch_20250910'
        assert record['name'] == RECORD_TOOL_NAME
        assert 'tool_choice' not in call
        assert 0 < call['timeout'] <= build_limits_search.TIME_BUDGET

    def test_not_found(self):
        client = FakeClient(response(recorded([], found=False, notes='No code published online.'), stop_reason='tool_use'))

        result = search_build_limits(client, 'Smallville, KS', 'A')

        assert result.found is False
        assert result.limits == []
        assert result.notes == 'No code published online.'

    def test_a_paused_turn_is_continued(self):
        paused = response(fetched(CODE_URL, TABLE), stop_reason='pause_turn', fetches=1)
        client = FakeClient(paused, response(
            recorded([limit('front_setback', 30, 'ft', 'Minimum front yard (feet)  30  30')]), stop_reason='tool_use'))

        result = search_build_limits(client, 'Lexington, MA', 'RS')

        assert client.calls[1]['messages'][-1] == {'role': 'assistant', 'content': paused.content}
        # The page fetched in the paused turn still counts as evidence.
        assert len(result.limits) == 1

    def test_finishing_without_recording_gets_one_required_nudge(self):
        first = response(SimpleNamespace(type='text', text='The front yard is 30 feet.', citations=[]))
        client = FakeClient(first, response(recorded([], found=False), stop_reason='tool_use'))

        search_build_limits(client, 'Lexington, MA', 'RS')

        second = client.calls[1]
        assert second['tool_choice'] == {'type': 'tool', 'name': RECORD_TOOL_NAME}
        assert second['messages'][-2:] == [{'role': 'assistant', 'content': first.content},
                                           {'role': 'user', 'content': build_limits_search.NUDGE_PROMPT}]

    def test_never_recording_is_an_error(self):
        text = response(SimpleNamespace(type='text', text='Done.', citations=[]))

        with pytest.raises(LimitsSearchError):
            search_build_limits(FakeClient(text, text), 'Lexington, MA', 'RS')

    def test_out_of_time_stops_asking(self):
        ticks = iter([0, 0, 75])  # start, first request, then nearly out of budget
        client = FakeClient(response(stop_reason='pause_turn'))

        with pytest.raises(LimitsSearchError):
            search_build_limits(client, 'Lexington, MA', 'RS', clock=lambda: next(ticks))

        assert len(client.calls) == 1

    def test_api_failure(self):
        with pytest.raises(LimitsSearchError):
            search_build_limits(FakeClient(FakeAPIError()), 'Lexington, MA', 'RS')

    def test_mock_client_runs_the_real_checks(self):
        result = search_build_limits(MockLimitsClient(), 'Mockville, TX', 'R-1')

        assert [item['key'] for item in result.limits] == [
            'front_setback', 'side_setback', 'rear_setback', 'max_height', 'max_lot_coverage', 'min_lot_area']
        assert result.dropped == [{'key': 'max_stories', 'value': 3, 'reason': 'quote not found on the page it cites'}]


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


class TestConfiguration:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for key in ('DATABASE_URL', 'RENTCAST_MOCK', 'ANTHROPIC_API_KEY', 'BUILD_LIMITS_MODEL',
                    'BUILD_LIMITS_DAILY_LIMIT'):
            monkeypatch.delenv(key, raising=False)

    def test_mock_by_default_locally(self):
        assert limits_mock_enabled() is True
        assert isinstance(build_limits_client(), MockLimitsClient)

    def test_never_mock_where_a_database_url_is_set(self, monkeypatch):
        monkeypatch.setenv('DATABASE_URL', 'postgresql+pg8000://example')
        monkeypatch.setenv('RENTCAST_MOCK', '1')

        assert limits_mock_enabled() is False

    def test_no_client_without_a_key(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '0')

        assert build_limits_client() is None
        assert limits_configured() is False

    def test_real_client_with_a_key(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '0')
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')

        client = build_limits_client()

        assert isinstance(client, anthropic.Anthropic)
        assert client.max_retries == 0
        assert limits_configured() is True

    def test_model_defaults_to_sonnet(self, monkeypatch):
        assert limits_model() == 'claude-sonnet-5'
        monkeypatch.setenv('BUILD_LIMITS_MODEL', 'claude-opus-5')

        assert limits_model() == 'claude-opus-5'

    @pytest.mark.parametrize('value, expected', [(None, 5), ('', 5), ('many', 5), ('2', 2)])
    def test_daily_limit(self, monkeypatch, value, expected):
        if value is not None:
            monkeypatch.setenv('BUILD_LIMITS_DAILY_LIMIT', value)

        assert daily_search_limit() == expected


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


class TestRunLookup:
    @pytest.fixture
    def user(self, app):
        user = User(username='me', email='me@example.com', password_hash='x')
        db.session.add(user)
        db.session.commit()
        return user

    def test_a_found_search_is_saved(self, app, user):
        row = run_lookup(MockLimitsClient(), ('Mockville, TX', 'R-1'), user.id)

        assert row.status == 'found'
        assert (row.web_searches, row.web_fetches) == (1, 1)
        assert row.model == limits_model()
        assert latest_lookup('Mockville, TX', 'R-1') == row
        assert [item['key'] for item in row.limits] == [
            'front_setback', 'side_setback', 'rear_setback', 'max_height', 'max_lot_coverage', 'min_lot_area']
        assert [item['key'] for item in row.dropped] == ['max_stories']

    def test_every_value_failing_the_check_is_not_found(self, app, user):
        client = FakeClient(response(fetched(CODE_URL, TABLE),
                                     recorded([limit('rear_setback', 15, 'ft', 'Minimum rear yard (feet) 15')]),
                                     stop_reason='tool_use'))

        row = run_lookup(client, ('Lexington, MA', 'RS'), user.id)

        assert row.status == 'not_found'
        assert 'none matched the text of the page it cited' in row.notes

    def test_a_failure_is_saved_but_never_shown_as_a_result(self, app, user):
        row = run_lookup(FakeClient(FakeAPIError()), ('Lexington, MA', 'RS'), user.id)

        assert row.status == 'failed'
        assert latest_lookup('Lexington, MA', 'RS') is None

    def test_counts_every_attempt_by_this_user_in_the_last_day(self, app, user):
        other = User(username='other', email='other@example.com', password_hash='x')
        db.session.add(other)
        db.session.commit()
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
        db.session.add_all([
            BuildLimitsLookup(jurisdiction='A, MA', district='R', status='found', requested_by_user_id=user.id),
            BuildLimitsLookup(jurisdiction='B, MA', district='R', status='failed', requested_by_user_id=user.id),
            BuildLimitsLookup(jurisdiction='C, MA', district='R', status='found', requested_by_user_id=user.id,
                              created_at=old),
            BuildLimitsLookup(jurisdiction='D, MA', district='R', status='found', requested_by_user_id=other.id),
        ])
        db.session.commit()

        assert searches_in_last_day(user.id) == 2
