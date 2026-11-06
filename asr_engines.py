#!/usr/bin/env python3
"""
asr_engines.py -- the two-pass transcription layer for Apogee Radio Log.

Parakeet transcribes everything; medium.en is loaded on demand and runs a
second pass only where it earns its cost. Measured on 80 clips from this
receiver, 22-23 Oct:

  medium.en   4360 s decode for 2923 s audio   fixed cost 11.9 s/clip
  parakeet     394 s decode for 2923 s audio   fixed cost  -0.1 s/clip
  -> 11x overall, 30x on clips under 10 s, and median time-to-transcript
     for ordinary traffic falls from 24.8 s to 2.4 s.

  medium.en invented text on 12 of 80 clips, all under 11 s: "...",
  "Thank you.", "The End", "you", "I'm sorry, I'm not going to say anything."
  Parakeet invented nothing on any of the 80.

  On coordinate digits medium.en is better, 4 clear wins to 1, so it keeps
  the coordinate job and Parakeet keeps the transcript. Different engines
  for different fields.

Why each piece is here
----------------------
Chunking at 30 s is not optional. Parakeet takes the whole clip in one pass
and encoder activations are quadratic in length: a 373 s broadcast cost
3.2 GB and was OOM-killed on an 8 GB Pi. Cuts land at the quietest point
near the boundary so a spoken coordinate is not split in half.

The second pass fires on two conditions, both measured:
  * a coordinate candidate -- fires on 20% of clips and missed none of the
    nine clips that actually contained coordinates. The obvious looser test
    ("mentions north/west/position") fires on 48% and saves almost nothing.
  * Parakeet returned nothing from audio that was readable -- it went silent
    on 17 of 80, and one of those was a real 5.5 s "Any VASCO, any VASCO,
    radio check channel 9". Costs 290 s across the sample to recover.

Prowords are normalised because Parakeet renders Securite as "Security",
"Secure day" and PAN PAN as "Ponton", "Pompon", "Bonbon". Those words are
safety classifications and the brief keys off them. Normalisation only fires
when the proword appears at least twice near the start, which is how radio
procedure actually says it -- so an ordinary sentence containing the word
"security" is left alone.

Adjudication never uses geographic plausibility. Every coordinate error
measured here was plausible: 1.1 km, 4.7 km and 28 m from truth, all of them
in the same patch of ocean off San Diego. find_positions already leans on
the home position to resolve a missing hemisphere and to reject distant
fixes, so a second plausibility prior would only compound that bias. The
rules used instead are agreement, stated-versus-assumed hemisphere, and the
dropped-decimal signature -- each of which discriminates on the errors that
actually occur.

This module changes nothing in watchkeeper. Run it directly to dry-run the
whole pipeline over recorded clips:

    ~/asr-bench/bin/python ~/asr_engines.py --preset boat --n 60
    ~/asr-bench/bin/python ~/asr_engines.py --preset boat --clips-from ~/compare_whisper.json
"""

import argparse
import gc
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
import tomllib
import zlib
from dataclasses import dataclass, field

import numpy as np
import soundfile as sf


AGREE_M = 100.0              # within this, two coordinates are the same fix
# The assumed-hemisphere rule exists to catch a hemisphere the parser had to
# guess, and a wrong guess puts the point on the far side of an ocean. It must
# not arbitrate a digit-level disagreement: on id 12964 it preferred the
# engine that had misheard "330" as "30" and lost 4.7 km by it.
HEMISPHERE_M = 50_000.0
DEFAULT_CHUNK_S = 30.0
DEFAULT_IDLE_RELEASE_S = 900.0


# ===========================================================================
# watchkeeper import -- for find_positions/haversine_nm only
# ===========================================================================

def import_watchkeeper(path=None):
    import importlib.util
    if path is None:
        for cand in ("~/watchkeeper.py", "~/watchkeeper/watchkeeper.py",
                     "/opt/watchkeeper/watchkeeper.py", "./watchkeeper.py"):
            cand = os.path.expanduser(cand)
            if os.path.exists(cand):
                path = cand
                break
    if not path or not os.path.exists(path):
        sys.exit("watchkeeper.py not found -- pass --watchkeeper /path/to/it")
    spec = importlib.util.spec_from_file_location("watchkeeper", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["watchkeeper"] = mod
    spec.loader.exec_module(mod)
    return mod, path


# ===========================================================================
# Prowords
# ===========================================================================
# Variants are the ones actually observed from both engines on this
# receiver, not a guess at what a model might produce.

# Prowords, as this receiver's decoders actually render them. Built from
# 20 days of its own log, not from a guess at what a model might produce.
#
# Securite is the hard one: it is French, the models have no such word, and
# "say-cure-ee-tay" lands all over the place -- secure, secured, securitae,
# securitay, securitas, securitize, securesay, SecureTape, SecureTay,
# Carrot take, Carrotation, Air safe, Cara Tays, Curitay. Nearly all of them
# share a "secur" prefix, so one loose pattern covers the bulk and the
# phonetic strays go in the second tier.
#
# PAN PAN is far more stable, being two syllables of plosive and nasal:
# pan, pawn, pon, pom, bon, bonbon, ponpon, pompon, ponton, bonton -- and
# at the edges palm, fan, taun.
#
# TWO TIERS, and the difference matters. STRICT patterns are words that do
# not occur in marine voice by accident, so repetition at the head of a
# message is enough. LOOSE patterns are ordinary English -- "take care",
# "secure state", "palm", "fan" -- and matching those anywhere would be
# reckless, so they additionally require the "all stations" run to follow.
PROWORDS_STRICT = [
    # "secret\w*" catches "Secretary, Secretary, Secretary of Stations",
    # logged 27 Oct. NOT "sec\w*": "Sector" appeared 10 times in message heads
    # in one day, inside every Coast Guard station identification, and
    # rewriting those would corrupt the station name on almost every
    # broadcast received.
    ("SECURITE", re.compile(
        r"\b(?:sec[uo]\w*|secret\w*)(?:[\s,.-]+d[ae]ys?)?\b", re.I)),
    ("PAN PAN", re.compile(
        r"\b(?:p[ao]w?n[dgkpst]?|p[ao]m|b[ao]n[dgkpst]?|p[ao]d"
        # "Honton, Honton, Honton" opened a live EPIRB relay on 27 Oct. Not
        # an English word, and the strict tier still needs two occurrences.
        r"|[hpb][ao]nt[ao]n|p[ao]np[ao]n\w*|b[ao]nb[ao]n\w*"
        r"|p[ao]mp[ao]n\w*)\b", re.I)),
    ("MAYDAY", re.compile(r"\bm[ae]y[\s-]?day\b", re.I)),
]
PROWORDS_LOOSE = [
    ("SECURITE", re.compile(
        r"\b(?:carrot[\s-]*\w*|car[ai][\s-]*ta\w*|cur[ai]ta\w*"
        r"|take\s+care"
        r"|air\s*(?:safe\w*|traffic|pace|tays?)"
        r"|here'?s\s+(?:a|day)"
        # SECURITE is "say-cure-ee-tay": the final syllable lands as
        # "day" and the first as Thurs/care/chair/church. Logged 30 Oct
        # on five clips. A lone weekday cannot fire this -- the loose
        # tier needs a station run plus two hits in front of it.
        r"|(?:thurs|tues|satur|sun|mon|wednes|fri)day"
        # "say-cure-ee-tay" also lands as "territory", five times on
        # 4 Nov and never once as a real word. The (ship|six) form is one
        # alternative rather than two so the rewrite swallows the leading
        # fragment; neither word is ever matched on its own.
        r"|(?:ship|six)[\s,.'-]*territor\w*|territor\w*"
        r"|care[\s,.'-]*(?:takes?|tafs?|safes?|says?|states?|stays?)"
        r"|chair(?:man)?|church\w*|christian\w*"
        r"|(?:tak\w+|pick\w*)[\s,.'-]+your[\s,.'-]+(?:day|date|case)s?"
        r")\b", re.I)),
    ("PAN PAN", re.compile(r"\b(?:palm|fan|ta[uw]n\w*)\b", re.I)),
]

_UNIT = (r"(?:all|off|hall|south|aw|both|alta|l|hal+o|hel+o|leal|heal)[\s,.'-]*"
         r"(?:the[\s,.'-]+)?(?:stations?|states?|locations?|nations?"
         r"|spaces?|stization|tization)(?:'s)?")
_STATION_RUN = re.compile(
    r"%s(?:[\s,.!?'-]*(?:with|of|and|include[sd]?|is|as)?[\s,.!?'-]*%s)+"
    % (_UNIT, _UNIT), re.I)
_UNIT_RX = re.compile(_UNIT, re.I)
_REPEAT = re.compile(
    r"\b([a-z][a-z'-]{2,}(?:\s+[a-z][a-z'-]{1,})?)\b[\s,.!-]+\1\b", re.I)

PROWORD_HEAD = 160


GAP_MAX = 24                 # characters between adjacent repetitions


def _cluster(matches):
    """The run of adjacent repetitions, not every match in the head.

    A proword word can recur later in the preamble for innocent reasons --
    one real message reads "SECURITE, SECURITE, SECURITE San Diego, United
    States Coast Guard Securite San Diego", where the station name itself
    garbled into the proword. Spanning first match to last would delete
    "San Diego, United States Coast Guard" along with it.

    Procedure says the repetitions are consecutive, so a gap wider than a
    few characters of punctuation ends the preamble. The densest cluster
    wins, earliest on a tie.
    """
    best, run = [], []
    for mo in matches:
        if run and mo.start() - run[-1].end() > GAP_MAX:
            if len(run) > len(best):
                best = run
            run = []
        run.append(mo)
    return run if len(run) > len(best) else best


RUN_NEAR = 40                # how far past the preamble the run may start

_PUNCT = re.compile(r"[^\w]+")


def _rewrite(text, canon, matches, run):
    """Replace the whole preamble span in one go.

    Substituting each token separately is what left "PAN PAN Pond" and
    "SECURITE day" standing in the log: sub() rewrites what it matched and
    leaves the rest of the garble beside it. It is also not idempotent --
    "PAN" itself matches the pan pattern, so a second pass over this layer's
    own output doubles every token.

    Three repetitions is procedure, not a count of what was received; the
    decoder's token count is noise, and raw_transcript keeps the original.

    The separator between the preamble and the stations run is carried over
    from the original rather than assumed. Dropping it merged "SECURITE" into
    "all stations", and `sec[uo]\\w*` then matched "SECURITEall" on the next
    pass and ate the "all" -- a silent corruption that only showed up under
    an idempotency check.
    """
    start, last = matches[0].start(), matches[-1].end()
    preamble = ", ".join([canon] * 3)

    if run is None:
        return text[:start] + preamble + text[last:]

    gap = text[last:run.start()]
    keep = "".join(_PUNCT.findall(gap)).strip() or ","
    got = text[run.start():run.end()]
    n = len(_UNIT_RX.findall(got))
    want = ", ".join(["all stations"] * n)
    # An already-correct run is left byte for byte. Rewriting "All stations"
    # to "all stations" would mark 60 routine broadcasts a day as edited for
    # no gain, and the edit flag is meant to mean something.
    if " ".join(got.split()).lower().rstrip(".,") == want:
        want = got
    return text[:start] + preamble + keep + " " + want + text[run.end():]


def normalise_prowords(text):
    """Canonicalise a safety or urgency proword said at the head of a message.

    Returns (text, proword, unidentified).
    """
    if not text:
        return text, None, False
    head = text[:PROWORD_HEAD]

    for canon, rx in PROWORDS_STRICT:
        hits = _cluster(list(rx.finditer(head)))
        if len(hits) >= 2:
            run = _STATION_RUN.search(head, hits[-1].end())
            # A run far past the preamble is not part of it; the text in
            # between is content and must not be swallowed.
            if run and run.start() - hits[-1].end() > RUN_NEAR:
                run = None
            # A loose variant immediately in front of a confirmed cluster is
            # the first repetition, decoded differently from the other two:
            # "Here's a, SECURITE, SECURITE". Safe to absorb only because the
            # proword is already established by the strict match -- the loose
            # patterns are ordinary English and never establish it alone.
            for lcanon, lrx in PROWORDS_LOOSE:
                if lcanon != canon:
                    continue
                pre = [m for m in lrx.finditer(head[:hits[0].start()])
                       if hits[0].start() - m.end() <= GAP_MAX]
                if pre:
                    hits = [pre[0]] + hits
            return _rewrite(text, canon, hits, run), canon, False

    # One strict hit abutting a station run. The preamble is said three
    # times, so a single surviving occurrence means the other two were
    # mangled past recognition -- but a lone "security" sitting directly
    # in front of "all stations, all stations, all stations" is not
    # ordinary English, it is a damaged preamble. Measured over 2,660
    # transcripts (bakeoff decodes plus 24 h of live traffic): 24 firings,
    # every one a genuine garbled SECURITE, none on other traffic.
    # Logged 31 Oct.
    for canon, rx in PROWORDS_STRICT:
        hits = _cluster(list(rx.finditer(head)))
        if len(hits) != 1:
            continue
        run = _STATION_RUN.search(head, hits[0].end())
        if run and run.start() - hits[0].end() <= RUN_NEAR:
            return _rewrite(text, canon, hits, run), canon, False

    run = _STATION_RUN.search(head)
    if run:
        for canon, rx in PROWORDS_LOOSE:
            hits = _cluster([m for m in rx.finditer(head)
                             if m.start() < run.start()])
            if len(hits) >= 2:
                return _rewrite(text, canon, hits, run), canon, False
        if _REPEAT.search(head[:run.start()]):
            return text, None, True

    return text, None, False


# Coast Guard units on this coastline that are NOT a sector. A rewrite that
# turns one of these into "Sector" asserts something false, so they are
# never touched. "air" alone is not here: there is no Air Sector, so
# "Coast Air Sector" is a mangled "Guard" -- but "Air Station" is real.
_NOT_A_SECTOR = {"station", "base", "cutter", "auxiliary", "helicopter",
                 "rescue", "vessel", "boat", "airstation"}
_SECTOR_PLACES = {
    "san diego": "San Diego", "los angeles": "Los Angeles",
    "long beach": "Long Beach", "san francisco": "San Francisco",
    "la": "LA", "l.a.": "LA", "l a": "LA",
}
_STATION_FIX = re.compile(
    r"\bcoast\s+(\w+)(?:\s+(\w+))?\s+"
    r"(san\s+diego|los\s+angeles|long\s+beach|san\s+francisco|l\.?\s?a\.?)\b",
    re.I)
_CORRECT = {("guard", "sector")}


def correct_station(text):
    """Repair a mangled Coast Guard sector name, anchored on the place.

    "Coast Guard Sector <place>" decodes correctly 77% of the time and
    mangles into 253 distinct forms over 18,579 logged messages -- a tail
    too long to enumerate, and still growing ("protector", 69, first seen
    30 Oct). The place is the anchor: San Diego, Los Angeles, Long Beach
    and San Francisco are the real sector names for this coastline, so a
    "Coast <something> <place>" is a sector with very high prior.

    Returns (text, fixes). Each fix is (original, replacement) so the
    caller can flag it -- the correction is an inference, not something
    the decoder said, and it is never applied silently.
    """
    if not text:
        return text, []
    fixes = []

    def sub(m):
        a = (m.group(1) or "").lower()
        b = (m.group(2) or "").lower()
        place = _SECTOR_PLACES.get(re.sub(r"\s+", " ", m.group(3).lower()))
        if place is None:
            return m.group(0)
        # a real, different unit -- leave it exactly as decoded
        if a in _NOT_A_SECTOR or b in _NOT_A_SECTOR:
            return m.group(0)
        if (a, b) in _CORRECT:
            return m.group(0)
        # "Coast Guard San Diego" with no middle word is not a mangle:
        # nothing was garbled, the unit name is simply shortened, which is
        # how people address them on the radio. Inserting "Sector" would
        # edit what was said rather than repair what was decoded. Logged
        # 31 Oct on three vessel-to-Coast-Guard exchanges, e.g. "Roger,
        # Coast Guard Los Angeles".
        if a == "guard" and not b:
            return m.group(0)
        # "Coast Guards Sector" -> "Coast Guard Sector": a plural, not a
        # mangled unit name. Corrected without a flag.
        if (a, b) == ("guards", "sector"):
            return "Coast Guard Sector %s" % place
        new = "Coast Guard Sector %s" % place
        fixes.append((m.group(0), new))
        return new

    return _STATION_FIX.sub(sub, text), fixes

# ===========================================================================
# Frequency formatting
# ===========================================================================

# Marine VHF is 156-163 MHz, so a bare four-digit number in front of
# "megahertz" can only be that band with the decimal point lost: 1571 is
# 157.1 and nothing else. Parakeet reads the handoff digits correctly --
# ground-truthed on eight Ch22A broadcasts, zero digits wrong -- but runs
# them together on about half, which is what makes a correct reading look
# like a wrong one.
#
# This inserts the decimal and changes no digit, so a misheard 1577 stays
# 157.7 rather than being "corrected" to the channel it ought to be. That
# is deliberate: the log records what was transmitted, and one of the
# ground-truth clips had the operator herself give the wrong frequency.
#
# Measured over 2,964 transcripts: 40 rewrites, every one inside a
# frequency handoff, none a range or any other quantity. Four-digit values
# outside the band (0571, 3157, 1001) are left alone.
_MHZ_RUNON = re.compile(r"\b(1(?:5[6-9]|6[0-3]))(\d)(\s*)(megahertz|mhz)\b", re.I)


def normalise_frequency(text):
    """Restore the decimal point in a run-together marine VHF frequency.

    Returns (text, fixes) where each fix is (original, replacement).
    """
    if not text:
        return text, []
    fixes = []

    def sub(m):
        new = "%s.%s%s%s" % (m.group(1), m.group(2), m.group(3), m.group(4))
        fixes.append((m.group(0), new))
        return new

    return _MHZ_RUNON.sub(sub, text), fixes


# ===========================================================================
# Second-pass triggers
# ===========================================================================

# A number sitting next to a coordinate word. Two of these is the threshold:
# it fires on 20% of clips and missed none of the nine that carried a real
# coordinate.
_NEAR_NUM = re.compile(
    r"(?:\d[\d.,\-]*\s*(?:degrees?|decimal|minutes?|north|south|east|west)"
    r"|(?:degrees?|decimal|minutes?|position)\s*[:\-]?\s*\d)", re.I)

# Parakeet sometimes spells a whole coordinate out in words, which the rule
# above cannot see. Six number words plus a coordinate word catches it.
_WORD_NUM = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|niner|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\b",
    re.I)
_COORD_WORD = re.compile(r"\b(?:degrees?|minutes?|north|south|east|west)\b",
                         re.I)

# What counts as "the decoder gave us nothing useful". Deliberately short:
# this only decides whether to spend a second pass, never what to store.
_JUNK = re.compile(r"^(?:you|mm+[\s-]*h+m+|hm+|uh+|um+)?[\s.…,!?-]*$", re.I)


def is_empty_result(text):
    return not text or bool(_JUNK.match(text.strip()))


def needs_coordinate_pass(text, min_near=2, min_words=6):
    if not text:
        return False
    if len(_NEAR_NUM.findall(text)) >= min_near:
        return True
    return (len(_WORD_NUM.findall(text)) >= min_words
            and bool(_COORD_WORD.search(text)))


# ===========================================================================
# Coordinate adjudication
# ===========================================================================

def _frac_given(pos):
    """Did the readback actually supply a fractional minute?

    An exact .000 is the signature of a dropped "decimal": medium.en heard
    "54 decimal 7" and Parakeet heard "54, 7", which parses to 54.000 and is
    1.1 km wrong while looking perfectly well formed.
    """
    for key in ("lat", "lon"):
        if abs((abs(pos[key]) * 60.0) % 1.0) > 1e-6:
            return True
    return False


STRAY_M = 150_000.0          # how far from its own list a fix must sit


def drop_stray_positions(wk, wk_positions):
    """Remove a coordinate that disagrees with the rest of its own message.

    A hazard-area bulletin reads out a polygon: every corner is within a few
    tens of miles of the others. A fix hundreds of kilometres from the rest
    of its own list is a misheard degree, not a second area.

    Measured on id 14012, a 450 s Navy operating-area broadcast: the stored
    list held 32-35.000N 115-26.000W (inland, near Mexicali) alongside
    32-55.550N and 32-40.000N at 118-55W, which are 15 nm apart off San
    Clemente. The stray was 354 km from both.

    Deliberately requires BOTH conditions: far from the cluster, and a
    hemisphere the parser had to infer rather than one that was spoken. The
    assumed flag is what separates a misheard degree from a real fix a long
    way off -- on 14012 the stray was the only assumed entry in the list.
    Three or more coordinates, so there is a cluster to be an outlier from.
    """
    if len(wk_positions) < 3:
        return wk_positions, []
    kept, dropped = [], []
    for i, p in enumerate(wk_positions):
        others = [q for j, q in enumerate(wk_positions) if j != i]
        near = min(wk.haversine_nm(p["lat"], p["lon"], q["lat"], q["lon"])
                   * 1852.0 for q in others)
        if near > STRAY_M and p.get("assumed"):
            dropped.append(p)
        else:
            kept.append(p)
    if not kept:
        return wk_positions, []          # never return nothing
    return kept, dropped


def adjudicate(wk, primary, second, prefer="second"):
    """Pick between two engines' coordinates and say how sure we are.

    primary/second are find_positions() outputs (lists, possibly empty).
    Returns (positions, source, flags).

    No geographic plausibility test: see the module docstring. The rules
    below are ordered by how well they discriminated on measured errors.
    """
    flags = []
    p0 = primary[0] if primary else None
    s0 = second[0] if second else None

    # Name the engine. "one engine only" left no way to tell afterwards which
    # of the two had found the coordinate, which is the first thing you want
    # to know when checking one by hand.
    if p0 and not s0:
        return primary, "primary", ["coordinate from the primary engine only"]
    if s0 and not p0:
        return second, "second", ["coordinate from the second pass only"]
    if not p0 and not s0:
        return [], "none", []

    apart = wk.haversine_nm(p0["lat"], p0["lon"], s0["lat"], s0["lon"]) * 1852.0
    if apart <= AGREE_M:
        # Two different architectures reaching the same digits independently
        # is the strongest evidence available here.
        return second, "agreed", []

    # A broadcast that reads out a whole operating area gives a list, and
    # comparing only the first entry of each list invents disagreements: on
    # id 13301 the two engines shared four of five coordinates while their
    # first entries sat 2 km apart. If the sets intersect at all, this is one
    # broadcast heard twice, and the fuller reading is the better one.
    shared = sum(1 for a in primary for b in second
                 if wk.haversine_nm(a["lat"], a["lon"],
                                    b["lat"], b["lon"]) * 1852.0 <= AGREE_M)
    if shared and (len(primary) > 1 or len(second) > 1):
        keep = primary if len(primary) > len(second) else second
        which = "primary" if keep is primary else "second"
        return keep, "%s (%d of %d/%d coordinates shared)" % (
            which, shared, len(primary), len(second)), flags

    # Both read out several coordinates and none of them matched. That is a
    # long polygon bulletin one engine lost the thread of, not two readings
    # of one fix -- and a garbled rendering yields FEWER parseable groups, so
    # the fuller list is the better reading. On id 14012 the default below
    # kept the second pass's three mangled corners over the primary's
    # coherent reading of the same 450 s broadcast.
    if len(primary) > 1 and len(second) > 1 and len(primary) != len(second):
        keep = primary if len(primary) > len(second) else second
        which = "primary" if keep is primary else "second"
        flags.append("multi-coordinate broadcast, no corner shared; kept the "
                     "fuller reading (%d against %d)"
                     % (max(len(primary), len(second)),
                        min(len(primary), len(second))))
        return keep, "%s (fuller reading)" % which, flags

    flags.append("coordinate dispute: %s vs %s, %.0f m apart"
                 % (p0["label"], s0["label"], apart))

    # A stated hemisphere beats one the parser inferred from home -- but only
    # when the gap is big enough to actually be a hemisphere error.
    if apart >= HEMISPHERE_M:
        if p0.get("assumed") and not s0.get("assumed"):
            return second, "second (hemisphere stated)", flags
        if s0.get("assumed") and not p0.get("assumed"):
            return primary, "primary (hemisphere stated)", flags

    # A supplied fraction beats a missing one.
    pf, sf_ = _frac_given(p0), _frac_given(s0)
    if sf_ and not pf:
        return second, "second (fraction supplied)", flags
    if pf and not sf_:
        return primary, "primary (fraction supplied)", flags

    # Nothing separates them; fall back to the engine measured better on
    # digits, and leave the dispute flagged so the log shows both.
    if prefer == "primary":
        return primary, "primary (default)", flags
    return second, "second (default)", flags


# ===========================================================================
# Chunking
# ===========================================================================

# Re-roll crops, tried in order after the configured one. They must be
# MAXIMALLY different from production and from each other: measured, crops
# that differ only slightly share a fate (400-trim and 400-trim-no-tail both
# failed; raw and tail-trim-only, 100 ms apart, both failed). Raw audio is
# deliberately absent -- it is in the same losing family and is what the old
# retry reached for.
#
#   name        head_trim  head_pad  tail_trim  tail_pad   (milliseconds)
REROLL_CROPS = (
    ("pad200",      0.0,     200.0,      0.0,     200.0),
    ("head200",   200.0,       0.0,    100.0,       0.0),
    ("head600",   600.0,       0.0,    100.0,       0.0),
)

# A transcript that ends on a station identification without a terminal
# proword. Measured: "...Coast Guard Sector Los Angeles, Long Beach" was
# truncated; "...Sector San Diego out" was complete. Without the sign-off
# test this fires on every correct broadcast, since they all end on the
# station. Catches truncation that is not sparse enough to trip the rate.
_STATION_TAIL = re.compile(r"long beach|coast guard|san diego|sector", re.I)
_TERMINAL_PW = re.compile(r"\b(out|over|standing by|clear)\b[.!]?\s*$", re.I)


def ends_mid_broadcast(text):
    t = (text or "").strip()
    return bool(_STATION_TAIL.search(t[-45:])) and not _TERMINAL_PW.search(t)


def n_words(text):
    return len(re.findall(r"[^\W_]+", text or "", re.UNICODE))


def compress_ratio(text):
    """Repetition measure. Real decodes 1.75-1.94; a looping decode 12.96.

    This replaces a characters-per-second ceiling, which measured the wrong
    thing: dense genuine speech is not a loop, and a 20 cps ceiling nearly
    rejected a correct 18.9 cps decode.
    """
    b = (text or "").encode("utf-8", "ignore")
    if len(b) < 24:
        return 1.0
    return len(b) / max(len(zlib.compress(b, 9)), 1)


def edge_treat(audio, sr, head_trim_ms, head_pad_ms,
               tail_trim_ms, tail_pad_ms):
    """Cut the head and tail, then pad each end with silence.

    Parakeet's encoder uses global attention, so an abrupt edge perturbs
    every frame rather than just the nearby ones. Captures open with a
    squelch burst and end mid-decay when the squelch closes. On row 19454
    treating only the front recovered nothing, while treating both ends
    recovered the proword and all three station identifications.

    Head and tail are separate because the artefacts are different: a burst
    at the start, a cut-off decay at the end.

    A trim is skipped entirely if the clip is too short to survive it, so a
    brief transmission is never made shorter than it already is.
    """
    if audio is None or len(audio) == 0:
        return audio
    ht = max(0, int(sr * float(head_trim_ms or 0) / 1000.0))
    tt = max(0, int(sr * float(tail_trim_ms or 0) / 1000.0))
    hp = max(0, int(sr * float(head_pad_ms or 0) / 1000.0))
    tp = max(0, int(sr * float(tail_pad_ms or 0) / 1000.0))
    if not (ht or tt or hp or tp):
        return audio
    if ht + tt and len(audio) <= ht + tt + int(0.5 * sr):
        ht = tt = 0                      # too short to trim safely
    body = audio[ht:len(audio) - tt] if (ht or tt) else audio
    if hp or tp:
        body = np.concatenate([
            np.zeros(hp, dtype=body.dtype), body,
            np.zeros(tp, dtype=body.dtype)])
    return body


def split_points(audio, sr, chunk_s, search_s=2.0, win_ms=100):
    """Chunk boundaries at the quietest moment near each nominal cut.

    ceil(duration / chunk_s) pieces of equal length, rather than filling
    pieces of chunk_s and leaving the remainder as a tail.

    Filling always produced one piece at the maximum length, and the maximum
    length is what fails: a 19.6 s clip at chunk_s=15 became 15.0 + 4.6, the
    15 s piece decoded to nothing, and the PAN PAN preamble it held was lost
    while the 4.6 s orphan came through. Split equally the same clip becomes
    2 x 9.8 s and both pieces decode.

    Measured over 14 clips on 27 Oct: 5 empty pieces against 6, and 6268
    characters against 5872. Slightly worse on three 40-second broadcasts,
    which is the cost of never giving any piece the full length.

    This also splits clips that the old rule left whole -- anything above
    chunk_s now yields at least two pieces -- and that turned out to be where
    most of the gain was. On three clips of 15-18 s the second piece carried
    a complete sentence that a single pass had dropped from the tail.
    """
    n = len(audio)
    step = int(chunk_s * sr)
    if step <= 0 or n <= step:
        return [(0, n)]
    w = max(1, int(win_ms * sr / 1000))
    k = (n // w) * w
    if k < w:
        return [(0, n)]
    energy = (audio[:k].astype("float64") ** 2).reshape(-1, w).mean(axis=1)

    pieces = int(math.ceil(n / float(step)))
    span = n / float(pieces)
    cuts = []
    for i in range(1, pieces):
        posn = int(i * span)
        lo = max(1, int((posn - search_s * sr) // w))
        hi = min(len(energy) - 1, int((posn + search_s * sr) // w))
        cut = posn if hi <= lo else int((lo + int(np.argmin(energy[lo:hi]))) * w)
        # Keep the cuts in order even when two nominal points snap to the
        # same quiet moment, which happens on a clip with one long pause.
        if cuts and cut <= cuts[-1]:
            cut = min(n, cuts[-1] + int(span))
        cuts.append(cut)
    bounds, prev = [], 0
    for c in cuts + [n]:
        if c > prev:
            bounds.append((prev, c))
            prev = c
    return bounds


# ===========================================================================
# Result
# ===========================================================================

@dataclass
class Result:
    text: str = ""                     # what goes in the log
    proword: str = None
    engine: str = "parakeet"
    second_pass: bool = False
    second_reason: str = None
    second_text: str = None
    positions: list = field(default_factory=list)
    coord_source: str = "none"
    flags: list = field(default_factory=list)
    primary_s: float = 0.0
    second_s: float = 0.0
    chunks: int = 1

    @property
    def decode_s(self):
        return self.primary_s + self.second_s


# ===========================================================================
# The layer
# ===========================================================================

class Engines:
    """Parakeet always; medium.en on demand, and released when idle.

    Both models resident is about 3.4 GB against Parakeet's 1.9 GB alone,
    which fits on an 8 GB Pi but leaves little headroom beside the
    channelizer -- hence the idle release.
    """

    def __init__(self, wk, home=None, max_nm=500.0,
                 parakeet_model="nemo-parakeet-tdt-0.6b-v2",
                 whisper_model="medium.en", quant="int8",
                 model_path=None,
                 chunk_s=DEFAULT_CHUNK_S, cpu_threads=2, beam_size=2,
                 head_trim_ms=0.0, head_pad_ms=0.0,
                 tail_trim_ms=0.0, tail_pad_ms=0.0,
                 retry_min_s=0.0, retry_min_cps=0.0, retry_gain=1.5,
                 reroll=True, reroll_min_cps=10.0, reroll_margin=1.5,
                 reroll_max_compress=3.0, reroll_min_s=4.0,
                 idle_release_s=DEFAULT_IDLE_RELEASE_S,
                 second_pass_on_empty=True, second_pass_min_s=None,
                 log=None):
        self.wk = wk
        self.home = home
        self.max_nm = max_nm
        self.parakeet_model = parakeet_model
        self.whisper_model = whisper_model
        self.quant = quant
        # A plain directory holding the model and its external .data file.
        # The Hugging Face cache cannot be used directly for the unquantised
        # weights: every file there is a symlink into blobs/<prefix>/, and
        # ONNX Runtime resolves the model's symlink and then refuses to follow
        # the .data out of that directory. See this patch's docstring.
        self.model_path = (os.path.expanduser(model_path)
                           if model_path else None)
        self.chunk_s = float(chunk_s or 0)
        # Edge treatment, applied once to the whole clip before any
        # chunking. See edge_treat() for why both ends matter.
        self.head_trim_ms = float(head_trim_ms or 0)
        self.head_pad_ms = float(head_pad_ms or 0)
        self.tail_trim_ms = float(tail_trim_ms or 0)
        self.tail_pad_ms = float(tail_pad_ms or 0)
        # Retry an implausibly sparse decode against the raw audio.
        # See _implausible() for the thresholds and why they exist.
        self.retry_min_s = float(retry_min_s or 0)
        self.retry_min_cps = float(retry_min_cps or 0)
        self.retry_gain = float(retry_gain or 1.5)
        # Staged re-rolling. See patch_reroll.py for the measurements.
        self.reroll = bool(reroll)
        self.reroll_min_cps = float(reroll_min_cps or 0)
        self.reroll_margin = float(reroll_margin or 1.5)
        self.reroll_max_compress = float(reroll_max_compress or 3.0)
        self.reroll_min_s = float(reroll_min_s or 0)
        self.cpu_threads = int(cpu_threads)
        self.beam_size = int(beam_size)
        self.idle_release_s = float(idle_release_s or 0)
        self.second_pass_on_empty = second_pass_on_empty
        # Floor for the empty-result second pass, in seconds of audio.
        #
        # Readability turned out to be the wrong axis: measured 23 Oct, a
        # 9.2 s Ch12 clip at 46.1 readability held real voice and the primary
        # engine returned nothing, so gating on quality locks the second pass
        # out of the cases it exists for. Duration separates them cleanly --
        # the four useless clips in the same sample were 0.7, 1.4, 1.6 and
        # 1.9 s, which are squelch pops, and that is where medium.en answers
        # with "Thank you." Overridden by SECOND_PASS_MIN_S in watchkeeper.
        self.second_pass_min_s = (
            float(second_pass_min_s) if second_pass_min_s is not None
            else float(getattr(wk, "SECOND_PASS_MIN_S", 2.5)))
        self.log = log
        self._pk = None
        self._wh = None
        self._wh_last_used = 0.0

    # ---------------------------------------------------------- model load
    def _parakeet(self):
        if self._pk is not None:
            return self._pk
        try:
            import onnx_asr
        except ImportError:
            sys.exit("onnx-asr is not installed in this interpreter:\n"
                     "    pip install 'onnx-asr[cpu,hub]'")
        last = None
        attempts = ([{"quantization": self.quant}]
                    if self.quant not in (None, "none") else []) + [{}]
        args = (self.parakeet_model,)
        if self.model_path:
            if not os.path.isdir(self.model_path):
                raise RuntimeError(
                    "primary_model_path %r is not a directory. It must hold "
                    "the model and its encoder-model.onnx.data side by side; "
                    "the Hugging Face cache will not work directly."
                    % self.model_path)
            args = (self.parakeet_model, self.model_path)
        for kw in attempts:
            try:
                self._pk = onnx_asr.load_model(*args, **kw)
                return self._pk
            except Exception as exc:                  # noqa: BLE001
                last = exc
        raise RuntimeError("cannot load %r through onnx-asr%s: %s"
                           % (self.parakeet_model,
                              " from %s" % self.model_path
                              if self.model_path else "", last))

    def _whisper(self):
        self._wh_last_used = time.time()
        if self._wh is not None:
            return self._wh
        from faster_whisper import WhisperModel
        kw = dict(device="cpu", compute_type="int8",
                  cpu_threads=self.cpu_threads)
        try:
            self._wh = WhisperModel(self.whisper_model,
                                    local_files_only=True, **kw)
        except Exception:                             # noqa: BLE001
            self._wh = WhisperModel(self.whisper_model, **kw)
        if self.log:
            self.log.info("second-pass model %s loaded", self.whisper_model)
        return self._wh

    def release_idle(self, now=None):
        """Drop the second-pass model if it has not been needed lately."""
        if self._wh is None or not self.idle_release_s:
            return False
        if (now or time.time()) - self._wh_last_used < self.idle_release_s:
            return False
        self._wh = None
        gc.collect()
        if self.log:
            self.log.info("second-pass model released after %.0fs idle",
                          self.idle_release_s)
        return True

    # ----------------------------------------------------------- decoding
    def _pk_once(self, audio, sr):
        m = self._parakeet()
        try:
            return (m.recognize(audio, sample_rate=sr) or "").strip()
        except TypeError:
            return (m.recognize(audio) or "").strip()

    def _run_pk(self, audio, sr):
        """Decode one array, splitting only if it exceeds chunk_s."""
        if not self.chunk_s or len(audio) <= self.chunk_s * sr:
            return self._pk_once(audio, sr), 1
        bounds = split_points(audio, sr, self.chunk_s)
        parts = [self._pk_once(audio[a:b], sr) for a, b in bounds]
        return " ".join(t for t in parts if t).strip(), len(bounds)

    def _implausible(self, text, secs):
        """Is this result too sparse to be a real transcript of this clip?

        Measured: good decodes run 10-17 characters per second of audio;
        the two known failures sat at 3.2 and 3.3. Below retry_min_s the
        start-point runaway does not occur, so a sparse result there is
        far more likely to be genuine near-silence than a lost opening.
        """
        if not self.retry_min_cps or secs < self.retry_min_s:
            return False
        return (len(text or "") / max(secs, 1e-9)) < self.retry_min_cps

    def _normalise(self, raw):
        """Proword, station and frequency repair, as one step.

        Lifted out of transcribe() so pass 1 can be normalised and written
        before refinement starts -- the log must not show "Pon Pon" for a
        few seconds and then correct itself.
        """
        text, pw, unknown = normalise_prowords(raw)
        text, station = correct_station(text)
        text, freq = normalise_frequency(text)
        return text, pw, unknown, station, freq

    def _crop_decode(self, audio, sr, crop=None):
        """One decode at one crop. crop=None means the configured one."""
        if crop is None:
            ht, hp, tt, tp = (self.head_trim_ms, self.head_pad_ms,
                              self.tail_trim_ms, self.tail_pad_ms)
        else:
            ht, hp, tt, tp = crop
        a = (edge_treat(audio, sr, ht, hp, tt, tp)
             if (ht or hp or tt or tp) else audio)
        return self._run_pk(a, sr)

    def _refine(self, audio, sr, first_raw, first_text, proword, n_chunks):
        """Re-roll a suspect decode and keep whichever says most.

        Returns (raw, chunks, flags). The first result is returned unchanged
        unless an alternative clears the word-count margin AND the loop guard.
        """
        flags = []
        secs = len(audio) / float(sr or 1)
        if not self.reroll or secs < self.reroll_min_s:
            return first_raw, n_chunks, flags

        # An empty pass 1 gives the comparison nothing to stand on: any
        # text a re-roll invents wins by default, because ten words beats
        # zero at any margin. Measured on a static Ch69 clip (quality 45.4,
        # unintelligible by ear) that returned nothing on pass 1 and "But
        # I'm not sure if I can do it" on all three crops.
        #
        # A readability floor cannot separate these -- at 50 it would drop
        # 41% of genuinely sparse traffic and still be a coin flip here.
        # Surviving text is what distinguishes a real recovery: m26953 had
        # 26 words before re-rolling to 51, m26981 had 9 before 62.
        #
        # Empty results keep second_pass_on_empty, which marks its output
        # 'low' precisely because that is where invention happens.
        if is_empty_result(first_text):
            return first_raw, n_chunks, flags

        rate = len(first_text or "") / max(secs, 1e-9)
        sparse = self.reroll_min_cps and rate < self.reroll_min_cps
        cut = ends_mid_broadcast(first_text)
        prio = (proword or "").upper() in ("MAYDAY", "PAN PAN")

        if not (sparse or cut or prio):
            return first_raw, n_chunks, flags

        # A truncated decode loses whatever part held the proword, so the
        # priority trigger cannot be relied on to fire for the clips that
        # need it most -- m26621 lost its preamble and so looked ordinary.
        # Anything suspect therefore gets every crop, not just one.
        why = ("priority proword %s" % proword if prio
               else "sparse %.1f cps" % rate if sparse
               else "ends mid-broadcast")

        best_raw, best_text = first_raw, first_text
        best_n, best_words = n_chunks, n_words(first_text)
        tried = []
        for name, ht, hp, tt, tp in REROLL_CROPS:
            try:
                raw, n = self._crop_decode(audio, sr, (ht, hp, tt, tp))
            except Exception as exc:                        # noqa: BLE001
                tried.append("%s failed" % name)
                continue
            text = self._normalise(raw)[0]
            w = n_words(text)
            tried.append("%s %dw" % (name, w))
            if w < best_words * self.reroll_margin:
                continue
            if compress_ratio(text) > self.reroll_max_compress:
                tried[-1] += " (repetitive, rejected)"
                continue
            best_raw, best_text, best_n, best_words = raw, text, n, w

        if best_words > n_words(first_text):
            # The original matters as much as the gain: a word count says
            # something was recovered, not that the first decode was wrong.
            snippet = (first_text or "").strip() or "(nothing)"
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            flags.append('re-rolled (%s): %d words -> %d [%s]; '
                         'pass 1 said: "%s"'
                         % (why, n_words(first_text), best_words,
                            ", ".join(tried), snippet))
            if self.log:
                self.log.info("re-roll %s: %d words -> %d (%s); pass 1 said: %r",
                              why, n_words(first_text), best_words,
                              ", ".join(tried), first_text or "")
        return best_raw, best_n, flags

    def _pk_text(self, audio, sr):
        # Treat the edges ONCE, on the whole clip, before any split. Doing
        # it per chunk would cut real audio out of every seam.
        treated = edge_treat(audio, sr, self.head_trim_ms, self.head_pad_ms,
                             self.tail_trim_ms, self.tail_pad_ms)
        text, n = self._run_pk(treated, sr)

        # The edge treatment is a net win often enough to keep as the
        # default, but it can collapse a clip that decodes perfectly raw --
        # measured 392 chars -> 75 on a 22.5 s Ch16 broadcast. When the
        # result is implausibly sparse, try the untouched audio and keep
        # whichever says more. This can only improve on the default.
        secs = len(audio) / float(sr or 1)
        if self._implausible(text, secs):
            alt, alt_n = self._run_pk(audio, sr)
            if len(alt) >= max(1, len(text)) * self.retry_gain:
                if self.log:
                    self.log.info(
                        "edge treatment produced %d chars over %.1fs "
                        "(%.1f/s); raw audio gave %d -- keeping raw",
                        len(text), secs, len(text) / max(secs, 1e-9), len(alt))
                return alt, alt_n
            if self.log:
                self.log.info(
                    "sparse result kept: %d chars over %.1fs (%.1f/s); "
                    "raw audio gave only %d", len(text), secs,
                    len(text) / max(secs, 1e-9), len(alt))
        return text, n

    def _wh_text(self, audio, sr, prompt=None, suppress=None):
        """Decode with the second-pass model, using the PRESET's settings.

        These are read from the watchkeeper module rather than hardcoded,
        because the preset's values are chosen for this receiver and several
        of them are the opposite of the library defaults. On preset boat:

          vad_filter = false          Silero rejects weak transmissions
                                      outright, which is precisely the audio
                                      this pass exists to recover. Hardcoding
                                      True would have made the pass useless
                                      on exactly the clips that triggered it.
          temperatures = [0.0]        each extra entry is a COMPLETE re-decode
                                      and is what produced the rtf=6 and
                                      rtf=9 outliers this config warns about.
          no_speech_threshold = 0.8   keeps more marginal speech
          log_prob_threshold = -1.5   likewise
          max_tokens_per_second = 5   bounds a runaway loop

        apply_preset_settings() has already written these into the module
        globals by the time a decode happens, so getattr picks up whatever
        the active preset says.
        """
        g = lambda name, default: getattr(self.wk, name, default)
        tps = g("MAX_TOKENS_PER_SECOND", 8)
        window = g("WHISPER_WINDOW_S", 30.0)
        cap = int(min(len(audio) / sr, window) * tps) + 16
        segments, _info = self._whisper().transcribe(
            audio,
            max_new_tokens=cap,
            beam_size=self.beam_size,
            temperature=g("WHISPER_TEMPERATURES", [0.0]),
            compression_ratio_threshold=g("COMPRESSION_RATIO_THRESHOLD", 2.4),
            log_prob_threshold=g("LOG_PROB_THRESHOLD", -1.0),
            no_speech_threshold=g("NO_SPEECH_THRESHOLD", 0.6),
            no_repeat_ngram_size=4,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            suppress_tokens=suppress if suppress is not None else [-1],
            vad_filter=g("VAD_FILTER", True),
            vad_parameters=dict(min_silence_duration_ms=500,
                                speech_pad_ms=300),
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def _positions(self, text):
        """Parse coordinates, then drop any that contradict their own list.

        Filtering here rather than after adjudication means each engine's
        reading is cleaned before the two are compared, so a misheard degree
        cannot become the "first position" that the comparison is made on.
        """
        if not text:
            return []
        clean = self.wk.strip_annotations(text)
        if not clean:
            return []
        found = self.wk.find_positions(clean, home=self.home,
                                       max_nm=self.max_nm)
        kept, dropped = drop_stray_positions(self.wk, found)
        if dropped and self.log:
            self.log.info("dropped %d coordinate(s) far from the rest of the "
                          "same message: %s", len(dropped),
                          ", ".join(p["label"] for p in dropped))
        return kept

    # ------------------------------------------------------------- public
    def transcribe(self, audio, sr, readable=True, prompt=None,
                   suppress=None, on_first=None):
        """Transcribe one transmission.

        `readable` is the caller's independent evidence that the recording
        carried audible speech -- watchkeeper's quality score. It only
        governs whether an empty result is worth a second pass.
        """
        res = Result()

        t0 = time.time()
        # Pass 1: the configured crop. Written to the database at once by
        # the on_first callback so the log stays current; refinement then
        # rewrites the row a few seconds later.
        raw, res.chunks = self._crop_decode(audio, sr, None)
        first_text, first_pw, _u1, _s1, _f1 = self._normalise(raw)
        if on_first:
            try:
                on_first(first_text)
            except Exception as exc:                        # noqa: BLE001
                if self.log:
                    self.log.warning("first-pass write failed: %s", exc)

        raw, res.chunks, _reroll_flags = self._refine(
            audio, sr, raw, first_text, first_pw, res.chunks)
        res.flags.extend(_reroll_flags)
        res.primary_s = time.time() - t0

        res.text, res.proword, unknown_pw = normalise_prowords(raw)
        if unknown_pw:
            res.flags.append('unidentified safety preamble -- listen to the recording')
        res.text, _station_fixes = correct_station(res.text)
        if _station_fixes:
            res.flags.append(
                'station name corrected: "%s" -> "Coast Guard Sector" '
                '(%d mention%s)'
                % (_station_fixes[0][0], len(_station_fixes),
                   "" if len(_station_fixes) == 1 else "s"))
        res.text, _freq_fixes = normalise_frequency(res.text)
        if _freq_fixes:
            res.flags.append(
                'frequency decimal restored: "%s" -> "%s"%s'
                % (_freq_fixes[0][0], _freq_fixes[0][1],
                   "" if len(_freq_fixes) == 1
                   else " (+%d more)" % (len(_freq_fixes) - 1)))
        primary_pos = self._positions(res.text)

        if needs_coordinate_pass(res.text):
            res.second_pass, res.second_reason = True, "coordinate candidate"
        elif (self.second_pass_on_empty and is_empty_result(res.text)
              and readable
              and len(audio) / float(sr) >= self.second_pass_min_s):
            res.second_pass, res.second_reason = True, "nothing from readable audio"

        if not res.second_pass:
            res.positions = primary_pos
            res.coord_source = "primary" if primary_pos else "none"
            return res

        t1 = time.time()
        try:
            res.second_text = self._wh_text(audio, sr, prompt, suppress)
        except Exception as exc:                      # noqa: BLE001
            res.flags.append("second pass failed: %s" % exc)
            res.second_text = None
        res.second_s = time.time() - t1

        if res.second_text is None:
            res.positions = primary_pos
            res.coord_source = "primary" if primary_pos else "none"
            return res

        res.engine = "parakeet+" + self.whisper_model

        if res.second_reason == "nothing from readable audio":
            # Parakeet heard nothing; whatever the second pass found is all
            # there is. Its own filter chain downstream decides whether that
            # is real -- this is exactly where medium.en invents, so the
            # text is handed on rather than trusted here.
            second_norm, pw, _ = normalise_prowords(res.second_text)
            res.text = second_norm
            res.proword = res.proword or pw
            res.positions = self._positions(second_norm)
            res.coord_source = "second" if res.positions else "none"
            res.flags.append("transcript from the second pass; "
                             "the primary engine returned nothing")
            return res

        # Coordinate candidate: the transcript stays Parakeet's, because it
        # reads better and does not invent. Only the coordinate is contested.
        second_pos = self._positions(res.second_text)
        res.positions, res.coord_source, flags = adjudicate(
            self.wk, primary_pos, second_pos, prefer="second")
        res.flags.extend(flags)
        return res


# ===========================================================================
# Dry run
# ===========================================================================

def load_settings(config_path, preset):
    with open(config_path, "rb") as fh:
        cfg = tomllib.load(fh)
    radio, storage = dict(cfg.get("radio", {})), dict(cfg.get("storage", {}))
    pre = dict(cfg.get("preset", {}).get(preset, {}))
    if not pre:
        sys.exit("preset %r not in %s; presets are: %s"
                 % (preset, config_path,
                    ", ".join(sorted(cfg.get("preset", {}))) or "(none)"))
    m = {}
    m.update(radio)
    m.update({k: v for k, v in pre.items()
              if not isinstance(v, (dict, list))})
    base = os.path.expanduser(storage.get("base_dir", "~/watchkeeper"))
    m["_audio_dir"] = os.path.expanduser(
        storage.get("audio_dir", os.path.join(base, "audio")))
    m["_db_path"] = os.path.expanduser(
        storage.get("db_path", os.path.join(base, "watchkeeper.db")))
    return m


def pick_clips(db_path, audio_dir, n, only_ids=None):
    uri = "file:%s?mode=ro" % db_path
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, channel, started_at, duration_s, audio_path, transcript "
        "  FROM messages WHERE audio_path IS NOT NULL "
        " ORDER BY started_at DESC").fetchall()
    con.close()
    live = {}
    for r in rows:
        p = r["audio_path"]
        if not os.path.isabs(p):
            p = os.path.join(audio_dir, p)
        if os.path.exists(p):
            d = dict(r)
            d["path"] = p
            live[r["id"]] = d
    if not live:
        sys.exit("no recordings on disk")
    if only_ids:
        out = [live[i] for i in only_ids if i in live]
        gone = len([i for i in only_ids if i not in live])
        if gone:
            print("  %d of %d pinned clips pruned from tmpfs"
                  % (gone, len(only_ids)))
    else:
        out = sorted(live.values(), key=lambda r: r["started_at"],
                     reverse=True)[:n]
    out.sort(key=lambda r: r["started_at"])
    return out


def ids_from(path):
    with open(path) as fh:
        d = json.load(fh)
    ids = []
    for per in (d.get("results") or {}).values():
        for r in per:
            if r.get("id") is not None and r["id"] not in ids:
                ids.append(r["id"])
    return ids


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.expanduser("~/watchkeeper.toml"))
    ap.add_argument("--watchkeeper", default=None)
    ap.add_argument("--preset", default="boat")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--clips-from", default=None)
    ap.add_argument("--parakeet-model", default="nemo-parakeet-tdt-0.6b-v2")
    ap.add_argument("--whisper-model", default="medium.en")
    ap.add_argument("--chunk-s", type=float, default=DEFAULT_CHUNK_S)
    ap.add_argument("--no-empty-pass", action="store_true",
                    help="do not second-pass clips the primary engine left "
                         "empty")
    ap.add_argument("--truth", default=None,
                    help="JSON of hand transcripts, {id: text}")
    ap.add_argument("--out", default=os.path.expanduser("~/two_pass_dryrun.json"))
    args = ap.parse_args()

    wk, wk_path = import_watchkeeper(args.watchkeeper)
    print("parser from %s" % wk_path)
    if not hasattr(wk, "expand_number_words"):
        print("NOTE: patch_wordnum.py is not applied -- coordinates read out "
              "in words will not parse")

    s = load_settings(args.config, args.preset)
    home = None
    if s.get("home_lat") is not None and s.get("home_lon") is not None:
        home = (float(s["home_lat"]), float(s["home_lon"]))
    truth = {}
    if args.truth:
        truth = {str(k): v for k, v in json.load(open(args.truth)).items()}

    clips = pick_clips(s["_db_path"], s["_audio_dir"], args.n,
                       ids_from(args.clips_from) if args.clips_from else None)
    print("scoring %d clips" % len(clips))

    eng = Engines(wk, home=home, max_nm=float(s.get("coord_max_nm", 500.0)),
                  parakeet_model=args.parakeet_model,
                  whisper_model=args.whisper_model,
                  chunk_s=args.chunk_s,
                  cpu_threads=int(s.get("cpu_threads", 2)),
                  beam_size=int(s.get("beam_size", 2)),
                  second_pass_on_empty=not args.no_empty_pass)

    rows, t_start = [], time.time()
    for i, c in enumerate(clips, 1):
        audio, sr = sf.read(c["path"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        try:
            r = eng.transcribe(audio, sr, readable=True)
        except Exception as exc:                      # noqa: BLE001
            print("  id %s failed: %s" % (c["id"], exc))
            continue
        t_text = truth.get(str(c["id"])) or truth.get(str(c["started_at"]))
        t_pos = eng._positions(t_text) if t_text else []
        rows.append(dict(
            id=c["id"], channel=c["channel"], started_at=c["started_at"],
            duration_s=round(len(audio) / sr, 2),
            text=r.text, proword=r.proword, engine=r.engine,
            second_pass=r.second_pass, second_reason=r.second_reason,
            second_text=r.second_text,
            positions=r.positions, coord_source=r.coord_source,
            flags=r.flags, primary_s=round(r.primary_s, 2),
            second_s=round(r.second_s, 2), chunks=r.chunks,
            log_transcript=c["transcript"],
            truth_text=t_text, truth_pos=t_pos))
        if i % 10 == 0 or i == len(clips):
            try:
                with open("/proc/self/statm") as fh:
                    rss = (int(fh.read().split()[1])
                           * os.sysconf("SC_PAGE_SIZE") / 1e6)
            except Exception:                         # noqa: BLE001
                rss = 0.0
            print("  %d/%d  (%.0fs elapsed, rss %.0f MB)"
                  % (i, len(clips), time.time() - t_start, rss), flush=True)

    # ------------------------------------------------------------- report
    audio_s = sum(r["duration_s"] for r in rows)
    prim = sum(r["primary_s"] for r in rows)
    sec = sum(r["second_s"] for r in rows)
    fired = [r for r in rows if r["second_pass"]]
    print("\n" + "=" * 74)
    print("COST   %d clips, %.0fs audio" % (len(rows), audio_s))
    print("  primary pass        %7.0fs   x%.2f realtime" % (prim, prim / max(audio_s, 1)))
    print("  second pass         %7.0fs   on %d clips (%.0f%%)"
          % (sec, len(fired), 100 * len(fired) / max(len(rows), 1)))
    print("  total               %7.0fs   x%.2f"
          % (prim + sec, (prim + sec) / max(audio_s, 1)))
    print("  all-whisper would be about %.0fs (11.9s/clip + 1.17 x audio)"
          % (11.9 * len(rows) + 1.17 * audio_s))
    by = {}
    for r in fired:
        by[r["second_reason"]] = by.get(r["second_reason"], 0) + 1
    for k, v in sorted(by.items()):
        print("    %-34s %d" % (k, v))

    quiet = [r for r in rows if not r["second_pass"]]
    if quiet:
        print("\n  time-to-transcript for traffic that triggers nothing "
              "(%d clips): median %.1fs"
              % (len(quiet), statistics.median(r["primary_s"] for r in quiet)))

    pw = [r for r in rows if r["proword"]]
    print("\n" + "=" * 74)
    print("PROWORDS normalised on %d clips" % len(pw))
    for r in pw[:10]:
        print("  id %-6s %-9s %s" % (r["id"], r["proword"], r["text"][:66]))

    print("\n" + "=" * 74)
    print("COORDINATES")
    src = {}
    for r in rows:
        src[r["coord_source"]] = src.get(r["coord_source"], 0) + 1
    for k, v in sorted(src.items(), key=lambda kv: -kv[1]):
        print("  %-28s %d" % (k, v))
    disputes = [r for r in rows if any("dispute" in f for f in r["flags"])]
    print("  disputes: %d" % len(disputes))
    for r in disputes[:12]:
        print("\n  --- id %s  %s" % (r["id"], r["started_at"]))
        for f in r["flags"]:
            print("      %s" % f)
        print("      kept: %s  (%s)"
              % (r["positions"][0]["label"] if r["positions"] else "none",
                 r["coord_source"]))

    scored = [r for r in rows if r["truth_pos"]]
    if scored:
        print("\n" + "=" * 74)
        print("AGAINST HAND TRANSCRIPTS (%d clips)" % len(scored))
        right = 0
        for r in scored:
            tp = r["truth_pos"][0]
            got = r["positions"][0] if r["positions"] else None
            d = (wk.haversine_nm(got["lat"], got["lon"], tp["lat"], tp["lon"])
                 * 1852.0) if got else None
            ok = d is not None and d <= AGREE_M
            right += ok
            print("  id %-6s truth %s   got %s %s"
                  % (r["id"], tp["label"],
                     got["label"] if got else "MISSED",
                     "ok" if ok else ("WRONG by %.0f m" % d) if d else ""))
        print("  correct: %d / %d" % (right, len(scored)))

    with open(args.out, "w") as fh:
        json.dump({"clips": len(rows), "preset": args.preset, "home": home,
                   "rows": rows}, fh, indent=1)
    print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
