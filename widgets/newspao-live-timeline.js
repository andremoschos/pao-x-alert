/* NewsPao Live Timeline v1 — SportScore free API. No invented events.
   One request per 120 seconds per open article; match locked to both opponents and date. */
(function () {
  'use strict';
  const root = document.querySelector('[data-np-live]');
  if (!root) return;
  const homeKey = root.dataset.home || '', awayKey = root.dataset.away || '';
  const teamSlug = root.dataset.team || '', day = root.dataset.date || '';
  const score = root.querySelector('[data-np-score]');
  const status = root.querySelector('[data-np-status]');
  const timeline = root.querySelector('[data-np-events]');
  const pitch = root.querySelector('[data-np-pitch]');
  const widget = root.querySelector('[data-sportscore="tracker"]');
  const placeholder = root.querySelector('[data-np-pitch-placeholder]');
  // Never cover a real SportScore tracker with the pre-match illustration.
  const syncPitch = () => { if (placeholder) placeholder.style.display='none'; };
  if (widget && typeof MutationObserver !== 'undefined') {
    new MutationObserver(syncPitch).observe(widget, {childList:true, subtree:true});
  }
  syncPitch();
  const api = 'https://sportscore.com';
  let matchSlug = root.dataset.matchSlug || '', fixture = null;
  if (!score || !status || !timeline || !day || !homeKey || !awayKey) return;
  const val = x => x && typeof x === 'object' ? String(x.name || x.short_name || x.full_name || x.display_name || '') : String(x ?? '');
  const normalize = x => val(x).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9α-ω]/g, '');
  const side = (m, k) => val(m?.[k] ?? m?.[k + '_team'] ?? m?.[k + 'Team'] ?? m?.teams?.[k] ?? m?.participants?.[k] ?? m?.[k + '_name']);
  const isMatch = m => {
    const h = normalize(side(m, 'home')), a = normalize(side(m, 'away'));
    return h.includes(normalize(homeKey)) && a.includes(normalize(awayKey));
  };
  const getSlug = m => {
    if (m.slug) return String(m.slug);
    const u = String(m.url || m.match_url || m.href || '');
    const match = u.match(/\/football\/match\/[^/]+\/([^/?#]+)/);
    return match ? match[1] : '';
  };
  const dated = m => {
    const s = String(m.date || m.time || m.start_time || m.startTime || m.kickoff || '');
    return !s || s.slice(0, 10) === day;
  };
  const matches = j => {
    const x = j.matches ?? j.fixtures ?? j.results ?? j.items ?? j.data?.matches ?? j.data?.fixtures ?? j.data?.items ?? j.data?.events ?? j.data ?? [];
    return Array.isArray(x) ? x : [];
  };
  async function get(path) {
    const r = await fetch(api + path, {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  function note(t) {
    timeline.replaceChildren();
    const p = document.createElement('p');
    p.style.cssText = 'margin:0;padding:10px 0;color:#5f7264';
    p.textContent = t;
    timeline.append(p);
  }
  function describe(e) {
    const t = normalize([e.type, e.event_type, e.eventType, e.kind, e.incidentType, e.incident_type, e.event?.name, e.detail, e.name, e.event_name].map(val).join(' '));
    const player = val(e.player || e.player_name || e.playerName || e.scorer || e.athlete || e.player?.name);
    const club = val(e.team || e.team_name || e.teamName);
    const p = player ? ' – ' + player : '';
    const c = club ? ' (' + club + ')' : '';
    if (/var/.test(t)) return '📺 Έλεγχος VAR' + c;
    if (/disallowedgoal|goaldisallowed|cancelledgoal|goalcancelled/.test(t)) return 'Ακυρώθηκε γκολ' + c;
    if (/missedpenalty|penaltymiss|penaltymissed|penaltysaved/.test(t)) return '❌ Χαμένο πέναλτι' + p + c;
    if (/owngoal|autogoal/.test(t) || e.own_goal===true) return '⚽ Αυτογκόλ' + p + c;
    if (/goal/.test(t) && !/goalkick|goalkeeper/.test(t)) return '⚽ ΓΚΟΛ' + (/penalty/.test(t)||e.penalty===true ? ' με πέναλτι' : '') + p + c;
    if (/penalty/.test(t)) return '🎯 Πέναλτι' + c;
    if (/secondyellow|yellowred|redcard|red/.test(t)) return '🟥 Κόκκινη κάρτα' + p + c;
    if (/yellow|booking/.test(t)) return '🟨 Κίτρινη κάρτα' + p + c;
    if (/substitution|substitut|playerswitch|subon|suboff/.test(t)) {
      const incoming = val(e.player_in || e.playerIn || e.in);
      const outgoing = val(e.player_out || e.playerOut || e.out);
      return '🔄 Αλλαγή' + (incoming ? ' – μέσα ' + incoming : '') + (outgoing ? ', έξω ' + outgoing : '') + (!incoming && !outgoing ? p : '') + c;
    }
    if (/bigchance|dangerousattack/.test(t)) return 'Μεγάλη ευκαιρία' + p + c;
    if (/shotsaved|shotontarget|shoton|save/.test(t)) return 'Σουτ στον στόχο / απόκρουση' + p + c;
    if (/shotofftarget|shotwide|shotoff/.test(t)) return 'Άστοχο σουτ' + p + c;
    if (/shot/.test(t)) return 'Τελική προσπάθεια' + p + c;
    if (/corner/.test(t)) return 'Κόρνερ' + c;
    if (/offside/.test(t)) return 'Οφσάιντ' + c;
    if (/freekick|foul/.test(t)) return 'Φάουλ' + c;
    if (/halftime/.test(t)) return 'Ημίχρονο';
    if (/fulltime|matchend/.test(t)) return 'Τέλος αγώνα';
    return '';
  }
  function minute(e) {
    let v = e.minute ?? e.elapsed ?? e.match_minute ?? e.time?.elapsed ?? e.time?.minute ?? e.time ?? e.clock ?? e.matchTime ?? '';
    if (v && typeof v === 'object') v = v.elapsed ?? v.minute ?? '';
    return String(v).trim().replace(/[′']/g,'');
  }
  function render(events) {
    const found = new Set(), rows = [];
    for (const e of events) {
      if (!e || typeof e !== 'object') continue;
      const m = minute(e), description = describe(e);
      if (!description || !/^\d{1,3}(\+\d{1,2})?$/.test(m)) continue;
      const key = m + '|' + description;
      if (found.has(key)) continue;
      found.add(key);
      rows.push({minute:m, text:description});
    }
    rows.sort((a,b) => parseInt(b.minute,10)-parseInt(a.minute,10));
    if (!rows.length) {note('Δεν έχουν καταγραφεί ακόμη διαθέσιμες φάσεις.');return;}
    timeline.replaceChildren();
    for (const row of rows) {
      const el = document.createElement('div');
      el.style.cssText = 'padding:12px 0;border-bottom:1px solid #e1ece5;line-height:1.55';
      const min = document.createElement('strong');
      min.style.cssText = 'color:#147144;margin-right:12px';
      min.textContent = row.minute + '′';
      el.append(min,document.createTextNode(row.text));
      timeline.append(el);
    }
  }
  function translateStatus(v) {
    const s = normalize(v);
    if (/finished|fulltime|ended/.test(s)) return 'Τελικό';
    if (/halftime|break/.test(s)) return 'Ημίχρονο';
    if (/live|inprogress|firsthalf|secondhalf/.test(s)) return 'LIVE';
    if (/postponed/.test(s)) return 'Αναβολή';
    if (/upcoming|notstarted|scheduled/.test(s)) return 'Πριν από τη σέντρα';
    return val(v) || 'Εξέλιξη αγώνα';
  }
  async function findMatch() {
    const paths = [
      '/api/v1/fixtures/?sport=football&date=' + encodeURIComponent(day) + '&limit=200',
      '/api/v1/fixtures/?sport=football&date=' + encodeURIComponent(day) + '&team=' + encodeURIComponent(teamSlug) + '&limit=200',
      '/api/widget/team/?sport=football&slug=' + encodeURIComponent(teamSlug) + '&limit=30',
      '/api/widget/matches/?sport=football&limit=50'
    ];
    for (const path of paths) {
      try {
        const list = matches(await get(path));
        const m = list.find(x => isMatch(x) && dated(x));
        if (m && getSlug(m)) return m;
      } catch (_) {}
    }
    return null;
  }
  async function update() {
    try {
      if (!matchSlug) {
        fixture = await findMatch();
        if (!fixture) {
          if (pitch) pitch.style.display = '';
          status.textContent = 'Αναμονή δεδομένων';
          note('Δεν έχει βρεθεί ακόμη επιβεβαιωμένη ροή για αυτόν τον αγώνα.');
          return;
        }
        matchSlug = getSlug(fixture);
      }
      const j = await get('/api/widget/match/?sport=football&slug=' + encodeURIComponent(matchSlug));
      const m = j.match ?? j.data?.match ?? j.data ?? j;
      if ((side(m,'home') || side(m,'away')) && !isMatch(m)) throw new Error('Wrong match');
      const hs = m.home_score ?? fixture?.home_score ?? m.score?.home ?? m.scores?.home;
      const as = m.away_score ?? fixture?.away_score ?? m.score?.away ?? m.scores?.away;
      if (hs !== undefined && as !== undefined && hs !== null && as !== null) score.textContent = hs + ' – ' + as;
      const currentStatus = m.status_text || m.status || fixture?.status_text || fixture?.status || '';
      status.textContent = translateStatus(currentStatus);
      if (pitch) pitch.style.display = '';
      syncPitch();
      let events = m.incidents ?? m.timeline ?? m.events ?? m.match_events ?? m.play_by_play ??
                   j.incidents ?? j.timeline ?? j.events ?? j.data?.incidents ?? j.data?.timeline ?? j.data?.events ?? [];
      if (!Array.isArray(events)) events = events.incidents || events.events || events.items || events.plays || [];
      render(Array.isArray(events)?events:[]);
    } catch (e) {
      status.textContent = 'Η ενημέρωση δεν είναι διαθέσιμη';
      note('Δεν έχουν ληφθεί επιβεβαιωμένες φάσεις από την πηγή.');
    }
  }
  update();
  setInterval(update, 120000);
})();