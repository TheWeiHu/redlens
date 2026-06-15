"""Tiny LDA (latent Dirichlet allocation) via collapsed Gibbs sampling.

Pure stdlib and deterministically seeded, sized for "what themes run
through a few thousand posts" at report-render time — not a library-grade
topic modeler. Vocabulary and document counts are capped so runtime stays
in seconds on year-scale topics.
"""
from __future__ import annotations

import random
from collections import Counter

from redlens import constants

K_TOPICS = constants.LDA_TOPICS
TOP_WORDS = constants.LDA_TOP_WORDS
VOCAB_SIZE = constants.LDA_VOCAB_SIZE
MAX_DOCS = constants.LDA_MAX_DOCS
ITERATIONS = constants.LDA_ITERATIONS
ALPHA = constants.LDA_ALPHA
BETA = constants.LDA_BETA
SEED = constants.LDA_SEED
MIN_DOC_TOKENS = 3  # documents shorter than this are dropped from the corpus


def topics(
    docs: list[list[str]], k: int = K_TOPICS, top_words: int = TOP_WORDS
) -> list[tuple[float, list[str]]]:
    """Discover ``k`` themes; returns (share of tokens, top words) per theme,
    largest first. Deterministic for a given corpus."""
    freq = Counter(w for d in docs for w in d)
    vocab = [w for w, _ in
             sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:VOCAB_SIZE]]
    word_id = {w: i for i, w in enumerate(vocab)}
    corpus = [ids for d in docs
              if len(ids := [word_id[w] for w in d if w in word_id]) >= MIN_DOC_TOKENS]
    if len(corpus) < k or not vocab:
        return []
    if len(corpus) > MAX_DOCS:
        corpus = corpus[::-(-len(corpus) // MAX_DOCS)]  # deterministic stride

    rng = random.Random(SEED)
    n_words = len(vocab)
    doc_topic = [[0] * k for _ in corpus]
    topic_word = [[0] * n_words for _ in range(k)]
    topic_total = [0] * k
    assignment: list[list[int]] = []
    for di, doc in enumerate(corpus):
        zs = []
        for w in doc:
            t = rng.randrange(k)
            zs.append(t)
            doc_topic[di][t] += 1
            topic_word[t][w] += 1
            topic_total[t] += 1
        assignment.append(zs)

    vbeta = n_words * BETA
    for _ in range(ITERATIONS):
        for di, doc in enumerate(corpus):
            dt = doc_topic[di]
            zs = assignment[di]
            for i, w in enumerate(doc):
                t = zs[i]
                dt[t] -= 1
                topic_word[t][w] -= 1
                topic_total[t] -= 1
                weights = [
                    (dt[j] + ALPHA) * (topic_word[j][w] + BETA)
                    / (topic_total[j] + vbeta)
                    for j in range(k)
                ]
                r = rng.random() * sum(weights)
                acc = 0.0
                for j, wt in enumerate(weights):
                    acc += wt
                    if r <= acc:
                        t = j
                        break
                zs[i] = t
                dt[t] += 1
                topic_word[t][w] += 1
                topic_total[t] += 1

    total = sum(topic_total) or 1
    themes = []
    for j in range(k):
        ranked = sorted(range(n_words),
                        key=lambda w: (-topic_word[j][w], vocab[w]))
        words = [vocab[w] for w in ranked[:top_words] if topic_word[j][w] > 0]
        if words:
            themes.append((topic_total[j] / total, words))
    themes.sort(key=lambda sw: (-sw[0], sw[1]))
    return themes
