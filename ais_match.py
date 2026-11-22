"""Decide whether a transmission names a vessel we can actually locate.

The rule here was not invented; it was measured. Run against 27,388 real
transcripts and the 678 vessels in range, a bare name match fired 957
times and was wrong most of the time -- "south swell" inside a Coast
Guard weather broadcast, "harbor master", Redondo and Cabrillo as place
names, "a great adventure out there today". Adding context gates cut
that to 251 keeps across 78 vessels, and spot-checking those found them
nearly all genuine hails.

So: a name alone is never enough. What separates a real hail from a
coincidence is the language around it.
"""

import difflib, math, re

# Single words that are vessel names AND ordinary speech. Each of these
# produced false hits. They are not banned outright -- being named
# outright ("motor vessel Master") still counts, and that rescued the
# real vessel Master from this list.
COMMON = {
 "leader","master","chief","adventure","eagle","magic","freedom","patriot",
 "serenity","vision","pelican","hammer","shannon","triumphant","challenge",
 "endeavour","endeavor","voyager","explorer","mariner","navigator","liberty",
 "legacy","destiny","harmony","grace","hope","faith","joy","dream","lucky",
 "sunshine","sunrise","sunset","island","pacific","intrepid","nomad",
 "wanderer","compass","anchor","swell","prime","solidarity","west","east",
 "north","south","time","star","spirit","wave","breeze","escape","odyssey"}

# Local geography. These appear constantly and are never the vessel.
PLACES = {"redondo","cabrillo","catalina","avalon","malibu","ventura",
          "oxnard","dana","newport","huntington","hermosa","manhattan",
          "palos","verdes","pedro","monica","marina","rey","angeles",
          "diego","barbara","clemente","nicolas","anacapa","mugu"}

# Vessels whose names contain local geography but which are hulls, not
# places. Without an entry here the PLACES guard below discards them
# outright, however plainly a transmission names them. Full names,
# lower case; add a line when another one turns up.
PLACE_OK = {
    "catalina provider",
    "catalina jet",
    "catalina flyer",
    "catalina king",
    "catalina duke",
}


# USCG safety traffic is long formulaic boilerplate thick with geography
# and weather. A name matched inside one is coincidence.
BROADCAST = re.compile(
    r"\b(securit[ey]|security,? security|safety broadcast|notice to mariners|"
    r"all stations, all stations|hazardous|weather|swell|advisory)\b", re.I)

# The ritual opening of a safety call, and nothing else: the repeated
# SECURITE (or PAN-PAN, or MAYDAY) that every such transmission starts
# with. Matched only at the very beginning, and only the ritual words --
# whatever follows is the message.
PREAMBLE = re.compile(
    r"^(?:[\s,.-]*(?:s[eé]curit[eéy]|pan[ -]?pan|mayday)\b)+[\s,.-]*", re.I)


def _after_preamble(tr, a):
    """Does the name begin the moment the safety preamble ends?

    Procedure for a vessel's own securite is SECURITE three times and
    then who is speaking, so her name sits exactly here. The Coast
    Guard's puts "all stations, all stations" in this slot instead,
    which is why this is a usable test and a bare BROADCAST check is
    not: suppressing every name inside a broadcast also suppressed
    every vessel announcing herself.
    """
    m = PREAMBLE.match(tr)
    return m is not None and m.end() > 0 and a == m.end()


# A real hail puts one of these near the name.
HAIL = re.compile(
    r"\b(this is|pilots?|channel \w+|calling|come back|go ahead|over|"
    r"roger|copy|standing by|call sign|inbound|outbound|underway)\b", re.I)

# Nearly every Ch12 transmission is a ship calling Long Beach Pilots, and
# the transcriber renders that second word as Polyps, Pylos, Part, Piles,
# Pirate, Pods or Policy -- 7 of 8 forms fail the test above. Only the
# second word varies, so match the station rather than the word. Measured
# over 27,445 transcripts: +5 hails, no false positives, restricted to
# multi-word vessel names (unrestricted it also claimed "Thank you sir.
# Cheers, Long Beach 5" for a vessel called CHEERS).
LONG_BEACH = re.compile(r"\blong beach \w+", re.I)

# --- tolerating a mistranscribed name -------------------------------------
# AIS says GIPSY DANGER and the transcriber heard "Gypsy Danger"; a hail
# says the name twice and one copy came through as "Laura Island". Fuzzy
# matching recovers those, but unguarded it was catastrophic: measured, it
# added 163 matches at 48% precision against a baseline of 96%, collapsing
# FIREBOAT 4 into "Fireboat 1" and matching JUST NED against "just need".
#
# Three guards, each aimed at one observed failure, bring that to 62
# additions across 22 vessels with no junk in the list:
#   * multi-word names only, with one substantial word
#   * digits must match exactly -- hull numbers are not typos
#   * every word within a length-scaled edit budget
# Off until a fuzzy span can be weighed against how ordinary its words
# are. See this patch's docstring for the measurement. Set True to
# restore the behaviour exactly as it was; nothing else was removed.
FUZZY_ENABLED = True
FUZZY_RATIO = 0.86       # overall similarity before any word is examined
MIN_KEY_LEN = 5          # a name needs one word this long to be identifiable
MIN_NAME_LEN = 7         # ...or enough letters across the whole name
WORD = re.compile(r"[A-Za-z0-9']+")

# --- how ordinary is the text a match claimed? ---------------------
# Built from this log itself: no outside word list can know that
# "securite" is common here and "polar" is not. Empty until the
# tagger loads it, and every gate below reads empty as "unknown"
# rather than "rare".
WORD_FREQ = {}
RARITY_FUZZY = 200      # a guess cannot afford an ordinary word
RARITY_EXACT = 1000     # an exact hit on a common phrase usually
                        # really is the vessel
PLAINWORD = re.compile(r"[A-Za-z']+")


def span_rarity(text):
    """Corpus count of the least common word in a claimed span."""
    if not WORD_FREQ:
        return None
    ws = [w.lower() for w in PLAINWORD.findall(text or "")]
    if not ws:
        return None
    return min(WORD_FREQ.get(w, 0) for w in ws)


# "Warship 76" is a particular unit, not the vessel WARSHIP -- the
# same reasoning the fuzzy guard already applies when it refuses to
# treat a hull number as a typo.
NUMBERED = re.compile(r"\s{0,2}\d")


def _numbered_unit(tr, end, name):
    return (not DIGITS.search(name)
            and NUMBERED.match(tr, end) is not None)
DIGITS = re.compile(r"\d")


def _dam(a, b):
    """Damerau-Levenshtein. A transposition is one edit, which is what
    "Gobel" -> "Goble" actually is; plain Levenshtein scores that two and
    would discard a real match."""
    da = {}
    la, lb = len(a), len(b)
    mx = la + lb
    d = [[mx] * (lb + 2) for _ in range(la + 2)]
    for i in range(la + 1):
        d[i + 1][1] = i
        d[i + 1][0] = mx
    for j in range(lb + 1):
        d[1][j + 1] = j
        d[0][j + 1] = mx
    for i in range(1, la + 1):
        db = 0
        for j in range(1, lb + 1):
            k = da.get(b[j - 1], 0)
            l = db
            c = 0 if a[i - 1] == b[j - 1] else 1
            if not c:
                db = j
            d[i + 1][j + 1] = min(d[i][j] + c, d[i + 1][j] + 1,
                                  d[i][j + 1] + 1,
                                  d[k][l] + (i - k - 1) + 1 + (j - l - 1))
        da[a[i - 1]] = i
    return d[la + 1][lb + 1]


def _budget(w):
    """How far one word may be off. Proportional, because two wrong
    letters in "maersk" is a plausible mishearing and two in "star" is a
    different word."""
    return 1 if len(w) <= 3 else int(0.34 * len(w))


# --- hearing rather than spelling ----------------------------------------
# The transcriber's errors are phonetic: it writes what the word sounded
# like. SHAY and SHEA are the same sound and two edits apart in letters,
# which is more than any safe edit budget for a four-letter word can
# allow -- at distance 2, SHAY also reaches STAR and SHED. Soundex
# compares what a word sounds like instead, so the budget stays tight
# and the homophone still matches.
_SOUNDEX = {}
for _ltrs, _code in (("BFPV", "1"), ("CGJKQSXZ", "2"), ("DT", "3"),
                     ("L", "4"), ("MN", "5"), ("R", "6")):
    for _c in _ltrs:
        _SOUNDEX[_c] = _code


def _sound(w):
    """Soundex key: first letter, then consonant sounds, vowels dropped.

    Deliberately coarse -- it is only ever consulted word by word inside
    _words_match, where every word must match, the word counts must
    agree, and the rarity gates still apply afterwards. A single coarse
    word is not enough to claim a name.
    """
    w = "".join(c for c in w.upper() if c.isalpha())
    if not w:
        return ""
    out = [w[0]]
    prev = _SOUNDEX.get(w[0], "")
    for c in w[1:]:
        code = _SOUNDEX.get(c, "")
        if code and code != prev:
            out.append(code)
        if c not in "HW":
            prev = code
    return ("".join(out) + "000")[:4]


MIN_SOUND_LEN = 3        # below this a sound key says almost nothing


def _close(a, b):
    """Same word, allowing for spelling or for hearing."""
    if _dam(a, b) <= _budget(a):
        return True
    return (len(a) >= MIN_SOUND_LEN and len(b) >= MIN_SOUND_LEN
            and _sound(a) == _sound(b))


def _words_match(name, seg):
    """Every word must match its counterpart, within its own budget.

    One good word is not enough: "Coast Guard, Pacific San Diego" matches
    PACIFIC STAR perfectly on "pacific" while "star"/"san" is nonsense.
    The exception is an anchor -- a long, distinctive word matching
    exactly -- which buys one sloppy neighbour, so "Maris Sequoia" still
    reaches MAERSK SEQUOIA on the strength of "sequoia".
    """
    nw = [w.lower() for w in _words(name)]
    sw = [w.lower() for w in _words(seg)]
    if (not any(len(w) >= MIN_KEY_LEN for w in nw)
            and sum(len(w) for w in nw) < MIN_NAME_LEN):
        # No word long enough to identify her on its own, and not
        # enough name in total either. MSC SHAY is 3 + 4 letters and
        # has neither a long word nor a short name: the transcriber
        # writes "MSC Shea", one transposition inside the per-word
        # budget, and the whole string is 0.875 similar -- every later
        # test passes, so this gate alone was the refusal. MV LI stays
        # refused, which is what the gate is for.
        return False
    if len(nw) != len(sw):
        return False
    if DIGITS.findall(name) != DIGITS.findall(seg):
        return False            # FIREBOAT 4 is not FIREBOAT 1
    anchor = any(len(a) >= 7 and a not in COMMON and a == b
                 for a, b in zip(nw, sw))
    over = sum(1 for a, b in zip(nw, sw) if not _close(a, b))
    return over <= (1 if anchor else 0)


_TOKS = {"tr": None, "toks": ()}


def _tokens(transcript):
    """Tokenise once per transcript, not once per vessel.

    match_all walks the whole fleet against one transcript, and this
    was being rebuilt inside that loop -- 1,283 identical
    tokenisations of the same string. One slot keyed on identity is
    enough: the caller finishes a transcript before the next.
    """
    if _TOKS["tr"] is not transcript:
        _TOKS["tr"] = transcript
        _TOKS["toks"] = [(m.group(0), m.start(), m.end())
                         for m in WORD.finditer(transcript)]
    return _TOKS["toks"]


def _fuzzy_span(transcript, v):
    """Where this vessel's name was nearly said, or None.

    A cheap gate runs first -- same word count, first word starting with
    the same letter and a similar length -- because the similarity ratio
    is the expensive part and this runs against the whole fleet."""
    nwords = len(v["words"])
    if nwords < 2:
        return None
    toks = _tokens(transcript)
    first = v["words"][0].lower()
    name = v["name"].lower()
    # One matcher per vessel, with the NAME as seq2: difflib caches
    # the b-sequence chain, and a fresh matcher per candidate threw
    # that away on every comparison.
    sm = difflib.SequenceMatcher(None, "", name)
    for i in range(len(toks) - nwords + 1):
        w0 = toks[i][0].lower()
        if not w0 or w0[0] != first[0] or abs(len(w0) - len(first)) > 2:
            continue
        a, b = toks[i][1], toks[i + nwords - 1][2]
        seg = transcript[a:b]
        sm.set_seq1(seg.lower())
        # Both quick ratios are upper bounds on ratio(), so refusing
        # on them cannot drop a span ratio() would have kept. They
        # only discard the hopeless ones sooner.
        if (sm.real_quick_ratio() < FUZZY_RATIO
                or sm.quick_ratio() < FUZZY_RATIO
                or sm.ratio() < FUZZY_RATIO):
            continue
        if _words_match(v["name"], seg):
            return a, b
    return None

# Ship types that introduce a name on this water. Deliberately not
# "pilot boat", "fireboat" or "sailboat": those are part of vessel
# names in this fleet (LB PILOT BOAT ALTAIR, FIREBOAT 4) and would
# swallow the name they precede.
TYPES = ("tanker|bulker|bulk carrier|freighter|container ship"
         "|containership|car carrier|cargo ship|barge|ferry"
         "|dredge|supply boat|crew boat")

MAX_POS_AGE_S = 3600          # the position must be under an hour old


def _words(n):
    return [w for w in re.split(r"[^A-Za-z0-9]+", n) if w]


# Station prefixes an AIS name carries and a radio never says. The feed
# calls her CGC ROBERT WARD; the air says "Coast Guard Cutter Robert
# Ward", "Cutter Robert Ward" or just "Robert Ward", and none of those
# contain the letters CGC. Matching the name with the prefix stripped
# catches all three at once, because the core sits inside every one of
# them.
NAME_PREFIX = {"CGC", "USCGC", "USCG", "USS", "USNS", "MV", "M/V", "MSV",
               "SV", "S/V", "FV", "F/V", "MT", "M/T", "RV", "R/V", "TS",
               "HSC", "PSV", "AHTS"}


def _core(name):
    """The name with a station prefix removed, if that leaves enough.

    Returns the name unchanged unless a prefix is there AND at least
    two words survive it. That floor is the point: stripping MV from
    MV PACIFIC leaves the single common word PACIFIC, which then tags
    on a bare "Pacific" through the address-position rule -- measured,
    and the reason this is not simply "strip any prefix". A two-word
    remainder like ROBERT WARD carries its own evidence; one word does
    not, so that vessel keeps her full AIS name and the old behaviour.
    """
    w = _words(name)
    if len(w) < 3:
        return name
    head = w[0].upper()
    if head not in NAME_PREFIX and head.replace("/", "") not in {
            x.replace("/", "") for x in NAME_PREFIX}:
        return name
    rest = name[name.upper().index(head, 0) + len(w[0]):].lstrip(" .,-/")
    return rest if len(_words(rest)) >= 2 and len(rest) >= 4 else name


# Checked against the text immediately BEFORE a match, so it also works
# for a fuzzy span -- the exact patterns in _strong cannot see a name the
# transcriber misspelled.
PRE_STRONG = re.compile(
    r"\b((motor |sailing |fishing |tug |service |chase |research |pilot )?"
    r"vessel|this is( the)?|(?:%s)[,:.]?)\s*$" % TYPES, re.I)


def _strong(tr, n):
    """The transmission names the vessel outright."""
    e = re.escape(n)
    return bool(
        re.search(r"\b(motor |sailing |fishing |tug |service )?vessel\s+%s\b"
                  % e, tr, re.I)
        or re.search(r"\bthis is\s+(the\s+)?%s\b" % e, tr, re.I)
        or re.search(r"\b%s[,.]?\s+%s\b" % (e, e), tr, re.I)
        # "Tanker Texas" identifies the ship as plainly as "Motor
        # Vessel Texas". Measured: +1 match in 29,081 transcripts,
        # and it is the one that was being missed.
        or re.search(r"\b(%s)[,:.]?\s+%s\b" % (TYPES, e), tr, re.I))


def haversine_nm(a_lat, a_lon, b_lat, b_lon):
    r = 3440.065                                  # earth radius, nm
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dl = math.radians(b_lon - a_lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compile_vessels(rows):
    """rows: (mmsi, name, lat, lon, pos_age_s[, sog]). Longest name
    first, so a vessel called GEM can never claim a transmission that
    said GEM SAPPHIRE.

    Speed is optional: a caller still using the five-column query
    keeps working, and its tags simply carry no drift.
    """
    out = []
    for row in rows:
        mmsi, name, lat, lon, age = row[:5]
        sog = row[5] if len(row) > 5 else None
        if not name or len(name) < 3:
            continue
        # Matched on the core, displayed and stored under the full AIS
        # name: the operator wants to see what the feed calls her.
        core = _core(name)
        out.append({"mmsi": mmsi, "name": name, "core": core,
                    "lat": lat, "lon": lon,
                    "pos_age_s": age, "sog": sog, "words": _words(core),
                    "re": re.compile(r"\b%s\b" % re.escape(core), re.I)})
    out.sort(key=lambda v: -len(v["name"]))
    return out


# Marine radio procedure is "<called>, <called>, this is <caller>". That
# makes "this is" more than a hail cue: it is the pivot that says which
# boat is which. Names before it are being called, names after it are
# speaking.
THIS_IS = re.compile(r"\bthis is\b", re.I)


def _role(transcript, start):
    piv = [m.start() for m in THIS_IS.finditer(transcript)]
    if not piv:
        return None
    return "from" if start > piv[0] else "to"


# Marine procedure opens with the station being called. A name first
# in the transmission and set off by punctuation is in that slot --
# and nothing else is, which is the whole value of the test.
VOCATIVE_PUNCT = re.compile(r"\s*[,.]")


def _vocative_at(tr, a, b):
    """Is the span [a, b) the station being addressed?

    First thing said, then either punctuation or the end of the
    transmission. That second half was missing, so a transcript that is
    nothing BUT a vessel name -- "Empire State", a station answering,
    routine on Ch12 -- failed every rule, while the same name followed
    by a comma tagged fine.
    """
    if tr[:a].strip():
        return False
    return (VOCATIVE_PUNCT.match(tr, b) is not None
            or not tr[b:].strip())


def _vocative(tr, m):
    """This match is the first thing said, and is addressed."""
    return _vocative_at(tr, m.start(), m.end())


# Three or more single CAPITAL letters running are one spoken
# initialism: "C S C L Atlantic Ocean" is CSCL ATLANTIC OCEAN. Capitals
# are required and three is the floor for the same reason -- allowing
# lower case let the run swallow the article in "a A N Tillett" and
# destroyed that match, and two letters would eat "M V Aries" and
# "I S that you".
LETTER_RUN = re.compile(r"\b(?:[A-Z][ .]){2,}[A-Z]\b")


def squash_initialisms(t):
    return LETTER_RUN.sub(
        lambda m: m.group(0).replace(" ", "").replace(".", ""), t)


# Below this corpus count for its rarest word, a multi-word name is
# distinctive enough to tag wherever it appears -- no hail word, no
# address position. See the header for the measurement, and for why
# single-word names are excluded from this rule entirely.
ANYWHERE_MAX = 50


def match_all(transcript, vessels, limit=4):
    """Every vessel named in this transcript that we can also place.

    A transmission routinely names two boats -- the one being called and
    the one calling -- so this returns a list. Matches are claimed
    longest-name-first and may not overlap, which stops GEM taking the
    text that belongs to GEM SAPPHIRE.

    `vessels` must already be filtered to those with a recent position:
    a vessel we cannot locate is not reported at all.
    """
    if not transcript or not transcript.strip():
        return []
    # A local copy. Offsets shift, but the only consumer of `at` is this
    # function's own ordering and role logic, which see the same text.
    transcript = squash_initialisms(transcript)
    # A broadcast is USCG boilerplate thick with geography and weather, so
    # a name merely MENTIONED inside one is coincidence -- "hazardous south
    # swell" is not the vessel SOUTH SWELL. But a vessel naming itself in a
    # securite is the opposite case, and common: "This is Chase Vessel Gipsy
    # Danger conducting unmanned surface vessel operations". So a broadcast
    # raises the bar to outright naming rather than rejecting the whole
    # transmission.
    in_bc = bool(BROADCAST.search(transcript))
    out, claimed = [], []

    def overlaps(a, b):
        return any(a < y and b > x for x, y in claimed)

    for v in vessels:                      # longest name first
        if (v["name"].strip().lower() not in PLACE_OK
                and any(w.lower() in PLACES for w in v["words"])):
            continue
        first = None
        for m in v["re"].finditer(transcript):
            if overlaps(m.start(), m.end()):
                continue
            if _numbered_unit(transcript, m.end(), v["name"]):
                continue
            rule = None
            if _strong(transcript, v["core"]):
                rule = "named outright"
            elif in_bc:
                if _after_preamble(transcript, m.start()):
                    # She is announcing herself. Given its own rule
                    # name so these can be audited apart from the rest.
                    rule = "broadcast self-ident"
                else:
                    continue
            elif _vocative(transcript, m):
                # No length floor and no COMMON check below this
                # branch, on purpose. Both were proxies for "is this
                # word being used as a name", and the address
                # position answers that directly.
                rule = "address position"
            else:
                a = max(0, m.start() - 60)
                b = min(len(transcript), m.end() + 60)
                ctx = transcript[a:b]
                hail = (HAIL.search(ctx)
                        or (len(v["words"]) >= 2
                            and LONG_BEACH.search(ctx)))
                if hail:
                    if len(v["words"]) >= 2:
                        rule = "multiword + hail"
                    elif v["words"][0].lower() in COMMON:
                        continue
                    elif len(v["name"]) >= 6:
                        rule = "distinctive + hail"
                elif len(v["words"]) >= 2:
                    # Distinctive enough to stand on its own anywhere in
                    # the sentence. Multi-word only: one rare word is not
                    # evidence that it is being used as a name, and the
                    # measurement is emphatic -- HAMMER took eleven
                    # transmissions about a claw hammer.
                    _r = span_rarity(transcript[m.start():m.end()])
                    if _r is not None and _r <= ANYWHERE_MAX:
                        rule = "distinctive anywhere"
                    else:
                        continue
                else:
                    continue
            if rule and (span_rarity(transcript[m.start():m.end()])
                         or 0) >= RARITY_EXACT:
                # Ordinary speech that happens to spell a name.
                rule = None
            if not rule:
                continue
            # Claim EVERY occurrence, not just the one reported. A hail
            # says the name twice, and leaving the second unclaimed lets
            # GEM take the back half of the second "Gem Sapphire".
            claimed.append((m.start(), m.end()))
            if first is None:
                first = (rule, m.start())
        if first is None and FUZZY_ENABLED and WORD_FREQ:
            # Nothing matched exactly. Try the name as the transcriber may
            # have mangled it -- but only for multi-word names, and only
            # through the guards in _words_match.
            #
            # Requires WORD_FREQ: the rarity gate below is the only
            # reason this is safe to run at all, so no table, no fuzzy.
            span = _fuzzy_span(transcript, v)
            if span and (span_rarity(transcript[span[0]:span[1]])
                         or 0) >= RARITY_FUZZY:
                # Ordinary speech, not a mangled name: "we will" is
                # not WET WILLY and "Good evening sir" is not
                # EVENING STAR.
                span = None
            if span and not overlaps(span[0], span[1]):
                a, b = span
                rule = None
                if PRE_STRONG.search(transcript[max(0, a - 40):a]):
                    rule = "named outright"
                elif _vocative_at(transcript, a, b):
                    # The rule the exact branch has always had. Its
                    # absence here meant a dropped letter lost the tag.
                    rule = "address position"
                elif not in_bc:
                    lo = max(0, a - 60)
                    hi = min(len(transcript), b + 60)
                    ctx = transcript[lo:hi]
                    if HAIL.search(ctx) or LONG_BEACH.search(ctx):
                        rule = "multiword + hail"
                    else:
                        # The rule the exact branch has, for the same
                        # reason: a name whose words the corpus almost
                        # never uses is being used as a name wherever it
                        # appears. Its absence here meant one dropped
                        # letter -- JABAL heard as JAMAL -- cost the tag
                        # unless a hail happened to sit beside it.
                        #
                        # Deliberately gated on ANYWHERE_MAX (50) rather
                        # than RARITY_FUZZY (200): this is the loosest
                        # path in the matcher, a guessed span with no
                        # supporting context, so it gets the strictest
                        # rarity floor.
                        _r = span_rarity(transcript[a:b])
                        if _r is not None and _r <= ANYWHERE_MAX:
                            rule = "distinctive anywhere"
                if rule:
                    claimed.append((a, b))
                    first = ("fuzzy: " + rule, a)
        if first:
            role = _role(transcript, first[1])
            if role is None and first[0] == "address position":
                # Procedure puts the called station first, so this is
                # known without a "this is" in the text to pivot on.
                role = "to"
            out.append(dict(v, rule=first[0], role=role, at=first[1]))
        if len(out) >= limit:
            break
    out.sort(key=lambda r: r["at"])         # as spoken
    return out


def match(transcript, vessels):
    """First/best vessel only. Kept for callers that want one."""
    r = match_all(transcript, vessels, limit=1)
    return r[0] if r else None
