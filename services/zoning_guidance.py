"""
Plain-language "what does this mean for a rebuild?" guidance for the
designations integrations/municipal_zoning.py finds on a parcel.

A zoning code and a list of overlay names ("SF-3-HD-NCCD-NP",
"Residential Design Standards") tell a user almost nothing on their own.
What they need to know is what each one *does* to a demolish-and-rebuild:
whether it can block the demolition outright, cap how big a house they can
put back, or add cost through construction requirements.

Deliberately qualitative. The exact by-right numbers — setbacks, height,
floor-to-area ratio, lot coverage — live in each jurisdiction's land
development code and are not published in any machine-readable form
(Austin's GIS doesn't carry them, its open data portal has no such
dataset, and the code itself sits behind a JavaScript-rendered viewer).
Rather than transcribe numbers that can't be verified against a primary
source — and Austin in particular amended its residential standards
recently, so secondhand figures are actively risky — this states what each
rule governs and points at the authority for the number. What a rule
*controls* is stable; what the number *is* changes with each amendment.
"""

# Keyed by the layer label integrations/municipal_zoning.py reports.
# 'governs' names the build parameters the rule controls, so a user knows
# which numbers they need to go look up for this parcel.
RESTRICTION_GUIDANCE = {
    'Local Historic Districts': {
        'means': 'Demolition and exterior changes need Historic Landmark Commission approval. '
                 'Full teardowns in a local historic district are frequently denied.',
        'governs': 'demolition permission, exterior design, materials',
    },
    'City of Austin Historic Landmarks': {
        'means': 'This property is individually landmarked. Demolition is generally prohibited '
                 'and any exterior work requires Historic Landmark Commission review.',
        'governs': 'demolition permission, exterior design',
    },
    'National Register of Historic Districts': {
        'means': 'Federal historic listing. It does not by itself block demolition, but it often '
                 'accompanies local rules that do, and it can affect financing and tax credits.',
        'governs': 'financing and tax treatment; often paired with local demolition limits',
    },
    'Neighborhood Conservation Combining District': {
        'means': 'Neighborhood-specific rules layered on top of base zoning. These commonly tighten '
                 'setbacks, height, and allowed footprint beyond what the base zoning would allow.',
        'governs': 'setbacks, height, footprint, sometimes design review',
    },
    'Residential Design Standards': {
        'means': "Austin's residential design standards (Subchapter F, the \"McMansion\" rules) cap "
                 'how much house you can build relative to lot size, and shape the allowed building '
                 'envelope. Usually the binding constraint on a rebuild\'s square footage.',
        'governs': 'floor-to-area ratio, building envelope, height planes',
    },
    'FEMA/City floodplain': {
        'means': 'Building in a mapped floodplain is restricted. Expect elevation requirements, '
                 'higher insurance, and in some cases no permit for new habitable structures.',
        'governs': 'whether you can build, required elevation, insurance cost',
    },
    'FEMA Special Flood Hazard Area': {
        'means': "FEMA's regulatory floodplain, with roughly a 1-in-100 chance of flooding in any year. "
                 'A rebuild here usually has to raise its lowest floor above the base flood elevation, '
                 'federally backed mortgages require flood insurance, and some places restrict new '
                 'construction outright.',
        'governs': 'whether and how you can rebuild, required floor elevation, insurance cost',
    },
    'FEMA moderate flood hazard': {
        'means': "Outside FEMA's regulatory floodplain but inside the 500-year flood area. Lower risk, "
                 'no mandatory flood insurance, and usually no special building rules, though '
                 'insurance is still worth pricing.',
        'governs': 'insurance cost, rarely what you can build',
    },
    'FEMA undetermined flood hazard': {
        'means': "FEMA hasn't studied flood risk here, so it's unknown rather than low. Check local "
                 'records; lenders and insurers may treat it cautiously.',
        'governs': 'insurance and lending, until the risk is mapped',
    },
    'Wildland Urban Interface 2024': {
        'means': 'Wildfire-hazard construction rules apply — fire-resistant materials and defensible '
                 'space. Adds cost to the rebuild rather than limiting its size.',
        'governs': 'construction materials, site clearance (cost, not size)',
    },
    'Capitol View Corridors': {
        'means': 'Protects sightlines to the State Capitol with a hard height ceiling that can be '
                 'well below what base zoning otherwise allows.',
        'governs': 'maximum height',
    },
    'Capitol Dominance Overlay': {
        'means': 'Height is capped to keep buildings below the Capitol.',
        'governs': 'maximum height',
    },
    'Hill Country Roadways Overlay': {
        'means': 'Scenic-roadway rules governing height, setbacks from the roadway, and clearing of '
                 'vegetation.',
        'governs': 'height, setbacks, tree and vegetation removal',
    },
    'Lake Austin Overlay': {
        'means': 'Shoreline rules with tighter impervious-cover limits and setbacks than base zoning.',
        'governs': 'impervious cover, setbacks',
    },
    'Waterfront Overlay': {
        'means': 'Waterfront rules affecting height, setbacks, and allowed uses near the water.',
        'governs': 'height, setbacks, allowed uses',
    },
    'Waterfront Setbacks Overlay': {
        'means': 'Additional setback from the waterfront, reducing the buildable area of the lot.',
        'governs': 'setbacks, buildable area',
    },
    'ETOD Overlay': {
        'means': 'Transit-oriented development overlay. Mostly permissive — it can allow more height '
                 'and density than base zoning if you opt in, usually with affordability conditions. '
                 'It does not raise what you can build by right.',
        'governs': 'optional extra height and density (opt-in, with conditions)',
    },
    'Urban Renewal Overlay': {
        'means': 'An urban renewal plan governs allowed uses and design here, overriding parts of base '
                 'zoning.',
        'governs': 'allowed uses, design standards',
    },
    'West Campus Neighborhood Overlay': {
        'means': 'University-area rules with their own density, height, and design standards.',
        'governs': 'density, height, design',
    },
    'Barton Springs Overlay': {
        'means': 'Watershed protection rules with strict impervious-cover and water-quality '
                 'requirements that can sharply limit buildable footprint.',
        'governs': 'impervious cover, water quality controls, footprint',
    },
    'Airport Overlay': {
        'means': 'Airport-proximity rules limiting height and imposing noise-attenuation construction '
                 'requirements.',
        'governs': 'maximum height, noise construction standards',
    },
    'Hazardous Pipelines': {
        'means': 'A hazardous pipeline easement crosses or abuts this parcel, restricting where '
                 'structures can be placed.',
        'governs': 'buildable area, structure placement',
    },
    'Neighborhood Planning Areas': {
        'means': 'An adopted neighborhood plan applies. It can carry its own land-use and design '
                 'expectations, and typically shapes how a rezoning or variance request is received.',
        'governs': 'land use policy, variance and rezoning outcomes',
    },
}

# Shown when a parcel has designations but none of them are recognized,
# and as the closing note in all cases.
GENERIC_NOTE = (
    'Exact setback, height, and floor-area limits depend on the base zoning district and any '
    'overlays above, and are set by the local land development code. Confirm them with the city '
    'before making an offer.'
)


def guidance_for(label):
    """The guidance entry for a restriction label, or None if unrecognized."""
    return RESTRICTION_GUIDANCE.get(label)


def annotate(restrictions):
    """
    Attach guidance to each restriction dict from
    MunicipalZoningResult.as_dict()['restrictions'], leaving the originals
    untouched. Unrecognized labels pass through without guidance rather
    than being dropped — a rule we can't explain is still a rule the user
    should know applies.
    """
    annotated = []
    for restriction in restrictions or []:
        entry = dict(restriction)
        entry['guidance'] = RESTRICTION_GUIDANCE.get(restriction.get('label'))
        annotated.append(entry)
    return annotated
