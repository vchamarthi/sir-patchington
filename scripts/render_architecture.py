"""Render the implemented architecture; no vendor assets or network required."""

from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODES = [
    (
        48,
        130,
        "1  Signed intake",
        "api.py · contracts.py",
        "GitHub webhook practices",
        ["Verify HMAC + enrolled identity", "Keep only routing metadata"],
        "#2563eb",
    ),
    (
        440,
        130,
        "2  Durable control plane",
        "store.py · cli.py",
        "Temporal idempotency ideas",
        ["PostgreSQL leases + fences", "Tenant quotas · one active PR"],
        "#2563eb",
    ),
    (
        832,
        130,
        "3  Scoped context",
        "context.py · gitwork.py",
        "Augment / Sourcegraph ideas",
        ["Revision + tenant + team scope", "Omissions and secret filtering"],
        "#2563eb",
    ),
    (
        832,
        390,
        "4  Investigate + repair",
        "pipeline.py · models.py",
        "Uber / Qodo / Stripe ideas",
        ["Review → fresh refutation", "Bounded minimal patch attempts"],
        "#7c3aed",
    ),
    (
        440,
        390,
        "5  Independent proof",
        "execution.py · probes.py",
        "Spotify / Meta testing ideas",
        ["Original fails · candidate passes", "External assertions · Docker limits"],
        "#0f766e",
    ),
    (
        48,
        390,
        "6  Frozen evidence",
        "artifacts.py · records.py",
        "Provenance / SLSA ideas",
        ["Patch + parent + tree + commit", "Immutable proof and log digests"],
        "#0f766e",
    ),
    (
        48,
        650,
        "7  Exact approval",
        "api.py · publisher.py",
        "GitHub authority boundaries",
        ["Authenticated scoped approver", "Exact proof · expiry · revocation"],
        "#b45309",
    ),
    (
        440,
        650,
        "8  Restricted publisher",
        "publisher.py · github.py",
        "gh-aw restricted-writer idea",
        ["Fresh fetch + reconstruction", "Expected-head CAS + readback"],
        "#b45309",
    ),
    (
        832,
        650,
        "9  Observe + rereview",
        "pipeline.py · cli.py",
        "GitHub checks / lifecycle",
        ["Pinned CI App + candidate SHA", "Base drift reverify · no merge"],
        "#2563eb",
    ),
]
parts = [
    '<svg xmlns="http://www.w3.org/2000/svg" width="1220" height="970" viewBox="0 0 1220 970">',
    '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#64748b"/></marker></defs>',
    '<rect width="1220" height="970" fill="#f8fafc"/>',
    '<g font-family="Arial, sans-serif">',
    '<text x="48" y="50" font-size="32" font-weight="700" fill="#0f172a">Sir Patchington: ideas → working modules</text>',
    '<text x="48" y="84" font-size="17" fill="#475569">Clean-room implementation. Source names identify inspiration, not copied code or equivalent quality.</text>',
]
for x1, y1, x2, y2 in [
    (390, 227, 440, 227),
    (782, 227, 832, 227),
    (1003, 325, 1003, 390),
    (832, 487, 782, 487),
    (440, 487, 390, 487),
    (219, 585, 219, 650),
    (390, 747, 440, 747),
    (782, 747, 832, 747),
]:
    parts.append(
        f'<path d="M{x1} {y1}L{x2} {y2}" stroke="#64748b" stroke-width="3" fill="none" marker-end="url(#arrow)"/>'
    )
for x, y, title, module, source, lines, color in NODES:
    parts.append(
        f'<rect x="{x}" y="{y}" width="342" height="195" rx="14" fill="white" stroke="#cbd5e1"/><rect x="{x}" y="{y}" width="7" height="195" rx="3" fill="{color}"/>'
    )
    for dy, text, size, weight, fill in [
        (35, title, 22, 700, "#0f172a"),
        (65, module, 15, 400, "#475569"),
        (97, source, 15, 700, color),
        (133, lines[0], 16, 400, "#334155"),
        (162, lines[1], 16, 400, "#334155"),
    ]:
        parts.append(
            f'<text x="{x + 22}" y="{y + dy}" font-size="{size}" font-weight="{weight}" fill="{fill}">{escape(text)}</text>'
        )
parts += [
    '<text x="48" y="901" font-size="18" font-weight="700" fill="#0f172a">Safe defaults: no comments or pushes · exact approval to publish · never auto-merge</text>',
    '<text x="48" y="933" font-size="16" fill="#475569">Locally exercised control boundaries. Live model quality, organization throughput and deployment reliability remain unqualified.</text>',
    "</g></svg>",
]
(ROOT / "docs/implementation-architecture.svg").write_text("\n".join(parts) + "\n")
