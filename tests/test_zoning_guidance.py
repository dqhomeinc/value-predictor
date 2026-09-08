from integrations.municipal_zoning import _CRITICAL_LAYERS, _HIGH_LAYERS
from services.zoning_guidance import RESTRICTION_GUIDANCE, annotate, guidance_for


class TestGuidanceCoverage:
    def test_every_flagged_layer_has_guidance(self):
        # A layer the lookup bothers to flag but can't explain leaves the
        # user with a bare code again — the exact problem this module
        # exists to fix. Adding a layer to municipal_zoning.py should mean
        # adding its explanation here.
        flagged = _CRITICAL_LAYERS | _HIGH_LAYERS
        missing = sorted(label for label in flagged if label not in RESTRICTION_GUIDANCE)

        assert missing == [], f'flagged layers with no guidance: {missing}'

    def test_every_entry_explains_both_meaning_and_what_it_controls(self):
        for label, entry in RESTRICTION_GUIDANCE.items():
            assert entry.get('means'), f'{label} has no plain-language explanation'
            assert entry.get('governs'), f'{label} does not say what it controls'

    def test_no_entry_states_a_specific_numeric_limit(self):
        # Guidance is deliberately qualitative: the by-right numbers aren't
        # available from any machine-readable source and change with each
        # code amendment, so stating one would risk being confidently
        # wrong. Guard that intent against future edits.
        import re
        for label, entry in RESTRICTION_GUIDANCE.items():
            text = f"{entry['means']} {entry['governs']}"
            numeric = re.findall(r'\b\d+\s*(?:ft|feet|foot|%|percent|sq)\b', text, re.IGNORECASE)
            assert not numeric, f'{label} states a specific limit ({numeric}) that cannot be verified'


class TestAnnotate:
    def test_attaches_guidance_to_known_labels(self):
        result = annotate([{'label': 'Local Historic Districts', 'severity': 'critical'}])

        assert result[0]['guidance']['means'].startswith('Demolition')
        assert 'demolition' in result[0]['guidance']['governs']

    def test_unknown_labels_pass_through_without_guidance(self):
        # A rule we can't explain is still a rule that applies — it must
        # not be silently dropped from the page.
        result = annotate([{'label': 'Some New Overlay', 'severity': 'high'}])

        assert len(result) == 1
        assert result[0]['label'] == 'Some New Overlay'
        assert result[0]['guidance'] is None

    def test_does_not_mutate_the_input(self):
        original = [{'label': 'Local Historic Districts', 'severity': 'critical'}]

        annotate(original)

        assert 'guidance' not in original[0]

    def test_handles_none_and_empty(self):
        assert annotate(None) == []
        assert annotate([]) == []

    def test_guidance_for_returns_none_for_unknown(self):
        assert guidance_for('Nonexistent Layer') is None
        assert guidance_for('Residential Design Standards') is not None
