from redlens import lda


def _corpus():
    cooking = ["recipe", "oven", "butter", "flour", "bake", "dough"]
    engines = ["engine", "turbo", "piston", "exhaust", "torque", "fuel"]
    docs = []
    for i in range(40):
        docs.append([cooking[(i + j) % 6] for j in range(8)])
        docs.append([engines[(i + j) % 6] for j in range(8)])
    return docs, set(cooking), set(engines)


def test_lda_separates_obvious_topics():
    docs, cooking, engines = _corpus()
    themes = lda.topics(docs, k=2, top_words=6)
    assert len(themes) == 2
    tops = [set(words) for _, words in themes]
    # each theme should be dominated by one vocabulary, not a blend
    assert any(t <= cooking for t in tops)
    assert any(t <= engines for t in tops)
    assert abs(sum(share for share, _ in themes) - 1.0) < 1e-9


def test_lda_is_deterministic():
    docs, _, _ = _corpus()
    assert lda.topics(docs, k=2) == lda.topics(docs, k=2)


def test_lda_degrades_gracefully():
    assert lda.topics([]) == []
    assert lda.topics([["one", "two"]]) == []          # too few docs/tokens
