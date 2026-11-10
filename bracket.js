/* トーナメント表の部品（公開ページと管理者用ページで共通）
 *
 *   const bk = TTBracket(要素, {
 *     mode: 'free' | 'next' | 'admin',
 *     ctx: { findTeam, winProb, esc, attachSuggest, allNames, eventNames, highlight },
 *     state: { text, event, date, note, results, picks, mine },   // 初期値
 *     official: { results },     // mode 'next' のときの公式結果（変更不可）
 *     onChange: state => {},     // 入力が変わるたびに呼ばれる（保存用）
 *   });
 *
 *   free  … 公開ページ「自分で作る」：組み合わせも勝者も自由に入れられる
 *   next  … 公開ページ「次の大会」：組み合わせと公式結果は固定、残りの試合で自分の予想を入れる
 *   admin … 管理者用ページ：組み合わせと実際の結果を入れる（CSV は呼び出し側で作る）
 */
(function () {
  const BYE = '__bye';
  const STAGES = ['優勝', '決勝', 'ベスト4', 'ベスト8', 'ベスト16', 'ベスト32'];

  const CSS = `
.tb textarea{width:100%;min-height:200px;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);box-sizing:border-box}
.tb input[type=date],.tb input[type=text]{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);min-width:0}
.tb .row{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px;align-items:center}
.tb .row .ac{flex:2 1 220px}
.tb .card+.card{margin-top:16px}
.tb .warn{background:var(--hl);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:10px 0;font-size:.9rem}
.tb .warn button,.tb .btn{font:inherit;font-size:.85rem;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer;margin:2px 4px 2px 0}
.tb .rounds{display:flex;gap:12px;overflow-x:auto;padding-bottom:8px;-webkit-overflow-scrolling:touch}
.tb .rcol{flex:0 0 200px}
.tb .rcol h3{font-size:.85rem;color:var(--sub);margin:0 0 6px;font-weight:600}
.tb .mc{border:1px solid var(--line);border-radius:8px;margin-bottom:8px;overflow:hidden;font-size:.88rem;background:var(--card)}
.tb .mc .pl{display:flex;justify-content:space-between;gap:6px;padding:7px 8px;cursor:pointer;border-bottom:1px solid var(--line)}
.tb .mc .pl:last-of-type{border-bottom:0}
.tb .mc .pl.win{background:var(--accent-soft);font-weight:700}
.tb .mc .pl.pick{background:repeating-linear-gradient(135deg,var(--accent-soft) 0 6px,transparent 6px 10px);font-weight:700}
.tb .mc .pl.lose{color:var(--sub)}
.tb .mc .pl.tbd,.tb .mc .pl.lock{cursor:default}
.tb .mc .pl.tbd{color:var(--sub)}
.tb .mc .pl span:last-child{color:var(--sub);font-variant-numeric:tabular-nums}
.tb .mc .ft{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:3px 8px;background:var(--bg);font-size:.75rem;color:var(--sub)}
.tb .mc .ft button{margin:0;padding:2px 8px;font-size:.78rem;font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer}
.tb .mc.wo{opacity:.6}
.tb .tag{font-size:.72rem;border:1px solid var(--line);border-radius:4px;padding:0 5px;background:var(--card);color:var(--ink)}
.tb .hit{color:var(--win);font-weight:700}.tb .miss{color:var(--lose);font-weight:700}
.tb .goal{font-size:1rem;margin:6px 0 12px}
.tb .goal b{font-size:1.15rem}
.tb .death td{background:var(--hl)}
.tb .legend2{display:flex;flex-wrap:wrap;gap:12px;font-size:.8rem;color:var(--sub);margin:4px 0 10px}
.tb .legend2 i{display:inline-block;width:22px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px;border:1px solid var(--line)}
`;
  function injectCss() {
    if (document.getElementById('tb-css')) return;
    const s = document.createElement('style'); s.id = 'tb-css'; s.textContent = CSS; document.head.appendChild(s);
  }

  function bigrams(s) { const a = []; for (let i = 0; i < s.length - 1; i++) a.push(s.slice(i, i + 2)); return a.length ? a : [s]; }
  function sim(a, b) {
    if (a === b) return 1;
    const A = bigrams(a), B = bigrams(b); let hit = 0; const used = B.slice();
    for (const x of A) { const k = used.indexOf(x); if (k >= 0) { hit++; used.splice(k, 1); } }
    let c = 0; for (const ch of new Set(a)) if (b.includes(ch)) c++;
    return Math.max(2 * hit / (A.length + B.length), c / Math.max(new Set(a).size, new Set(b).size) * 0.8);
  }
  function walk(node, f, depth = 0, path = []) {
    f(node, depth, path);
    if (!node.leaf) { walk(node.a, f, depth + 1, [...path, node]); walk(node.b, f, depth + 1, [...path, node]); }
  }

  /** 組み合わせの文字から出場校の並びを読む */
  function parse(text, findTeam) {
    const out = [];
    (text || '').split(/\r?\n/).forEach((raw, ln) => {
      let s = raw.normalize('NFKC').replace(/[　\t]/g, ' ').trim();
      if (!s) return;
      s = s.replace(/^([0-9]+|[A-H]|[IVX]+|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)[\s.．、:)]+/, '').replace(/\s+[0-9]+$/, '').trim();
      let wait = 0, wd = false;
      const m = s.match(/\*\s*([0-9])\s*$/); if (m) { wait = +m[1]; s = s.slice(0, m.index).trim(); }
      if (/(\s|^)(棄権|キ)$/.test(s)) { wd = true; s = s.replace(/(\s|^)(棄権|キ)$/, '').trim(); }
      const name = s.replace(/\s+/g, '');
      if (!name) return;
      const bye = /^(bye|不戦)$/i.test(name);
      const t = bye ? null : findTeam(name);
      out.push({ ln, raw, name: t ? t.name : name, input: name, team: t, wait, bye, wd });
    });
    return out;
  }
  /** 並びからトーナメントの木を組み立てる */
  function build(entries) {
    const warns = [];
    let items = entries.map((e, i) => ({ leaf: true, e, i, lo: i, hi: i, level: e.bye ? 0 : e.wait }));
    for (let r = 0; items.length > 1 && r < 12; r++) {
      const out = [];
      for (let i = 0; i < items.length; i++) {
        const it = items[i], nx = items[i + 1];
        if (it.level === r && nx && nx.level === r) {
          out.push({ leaf: false, a: it, b: nx, level: r + 1, lo: it.lo, hi: nx.hi, key: it.lo + '-' + nx.hi }); i++;
        } else {
          if (it.level === r) {
            it.level = r + 1;
            if (it.leaf && !it.e.bye) warns.push(`「${it.e.name}」は相手がいないので、${r + 1}回戦は不戦で進む扱いにしました（シードなら *${it.e.wait + 1} を付けると確実です）`);
          }
          out.push(it);
        }
      }
      items = out;
    }
    return { root: items[0] || null, warns };
  }

  window.TTBracket = function (el, opt) {
    injectCss();
    const { findTeam, winProb, esc, attachSuggest } = opt.ctx;
    const mode = opt.mode;
    const S = Object.assign({ text: '', event: '', date: '', note: '', results: {}, picks: {}, mine: '' }, opt.state || {});
    let official = (opt.official && opt.official.results) || {};
    let root = null, entries = [];

    const rOf = name => findTeam(name) || { rating: 1500, rd: 350 };
    // 古い保存形式 {side:'a'} を {w:学校名} に直す
    const winnerOf = (r, A, B) => { if (!r) return null; if (r.w) return r.w === A || r.w === B ? r.w : null; if (r.side) return r.side === 'a' ? A : B; return null; };

    /** 枠を勝ち上がった学校（学校名 / BYE / null=未定）。res は使う結果の表 */
    function decided(node, res) {
      if (node.leaf) return (node.e.bye || node.e.wd) ? BYE : node.e.name;
      const A = decided(node.a, res), B = decided(node.b, res);
      if (A === BYE && B === BYE) return BYE;
      if (A === BYE && B) return B;
      if (B === BYE && A) return A;
      if (A && B) return winnerOf(res[node.key], A, B);
      return null;
    }
    /** 各枠を勝ち上がる確率 {学校名: p}。node._d に残す */
    function dist(node, res, acc) {
      if (node.leaf) return { [(node.e.bye || node.e.wd) ? BYE : node.e.name]: 1 };
      const dl = dist(node.a, res, acc), dr = dist(node.b, res, acc);
      if (res) { const d = decided(node, res); if (d) { node._d = { [d]: 1 }; return node._d; } }
      const out = {};
      for (const [t, pt] of Object.entries(dl)) for (const [u, pu] of Object.entries(dr)) {
        const w = pt * pu;
        if (t === BYE) { out[u] = (out[u] || 0) + w; continue; }
        if (u === BYE) { out[t] = (out[t] || 0) + w; continue; }
        const p = winProb(rOf(t), rOf(u));
        out[t] = (out[t] || 0) + w * p; out[u] = (out[u] || 0) + w * (1 - p);
        if (acc) { acc[t] = (acc[t] || 0) + w * p; acc[u] = (acc[u] || 0) + w * (1 - p); }
      }
      node._d = out;
      return out;
    }
    // 結果の表：admin/free は S.results、next は公式結果（確率用）と、公式＋自分の予想（表示用）
    const probRes = () => mode === 'next' ? official : S.results;
    const viewRes = () => mode === 'next' ? Object.assign({}, S.picks, official) : S.results;
    const tapRes = () => mode === 'next' ? S.picks : S.results;

    // ---------- 画面の骨組み ----------
    const editable = mode !== 'next';
    const resultWord = mode === 'free' ? '結果・予想' : mode === 'admin' ? '実際の結果' : '予想';
    el.classList.add('tb');
    el.innerHTML = `
      ${editable ? `<div class="card">
        <h2 style="margin-top:0">組み合わせを入れる</h2>
        ${mode === 'admin' ? `<div class="row">
          <input type="text" class="f-event" autocomplete="off" placeholder="大会名（例：大阪高校総体（男子））">
          <input type="date" class="f-date">
          <input type="text" class="f-note" placeholder="備考（例：二次）" style="flex:1 1 120px">
        </div>` : ''}
        <textarea class="f-text" spellcheck="false" autocapitalize="off" autocorrect="off"
          placeholder="組み合わせ表の上から順に、1行に1校ずつ入れてください。&#10;上宮 *2&#10;大産大附&#10;bye&#10;泉陽&#10;…"></textarea>
        <p class="meta" style="margin:6px 0 0">・表の<b>上から順に</b>1行1校。左右2列の表は、左の列を上から入れてから右の列を続けます。<br>
          ・相手のいない枠は <b>bye</b>。bye の書かれていないシード校は、待つ回戦の数を <b>*1</b>・<b>*2</b> のように後ろに付けます。<br>
          ・棄権した学校は後ろに <b>棄権</b> と付けます（相手は不戦で勝ち上がり、試合には数えません）。<br>
          ・番号や空白は自動で取り除きます。PDF や写真の文字は、長押しでコピーして貼り付けられます。</p>
        <div class="o-warn"></div>
      </div>` : `<div class="card o-head"></div>`}

      <div class="card o-prob" hidden>
        <h2 style="margin-top:0">勝ち上がる確率</h2>
        <div class="tools"><label>自校 <select class="f-mine"></select></label></div>
        <div class="o-goal"></div>
        <div class="tbl"><table><thead class="o-phead"></thead><tbody class="o-pbody"></tbody></table></div>
        <p class="meta o-pnote"></p>
        <h2>ブロックの比較</h2>
        <div class="tbl"><table><thead><tr><th>ブロック</th><th>本命</th><th class="num">勝ち抜く確率</th><th class="num">上位3校の平均</th><th>主な学校</th></tr></thead><tbody class="o-blocks"></tbody></table></div>
        <p class="meta">上位3校の平均レーティングがいちばん高いブロックを「最激戦」と表示します。</p>
      </div>

      <div class="card o-tree" hidden>
        <h2 style="margin-top:0">${resultWord}を入れる</h2>
        <p class="meta o-tnote" style="margin-top:0"></p>
        <div class="o-score"></div>
        <div class="rounds o-rounds"></div>
        <div class="row" style="margin:8px 0 0"><button class="btn f-clear">${resultWord}をすべて消す</button></div>
      </div>

      <div class="card o-review" hidden>
        <h2 style="margin-top:0">振り返り</h2>
        <p class="meta o-rnote" style="margin-top:0"></p>
        <div class="tbl"><table><thead><tr><th>学校</th><th class="num">期待</th><th class="num">実際</th><th class="num">差</th></tr></thead><tbody class="o-rbody"></tbody></table></div>
      </div>`;
    const q = s => el.querySelector(s);

    let tm = null;
    const changed = () => { if (opt.onChange) opt.onChange(getState()); };
    if (editable) {
      q('.f-text').value = S.text;
      q('.f-text').addEventListener('input', () => { clearTimeout(tm); tm = setTimeout(() => { S.text = q('.f-text').value; render(); changed(); }, 300); });
      if (mode === 'admin') {
        q('.f-event').value = S.event; q('.f-date').value = S.date; q('.f-note').value = S.note;
        if (attachSuggest && opt.ctx.eventNames) attachSuggest(q('.f-event'), opt.ctx.eventNames.map(e => [e, '']));
        for (const [cls, key] of [['.f-event', 'event'], ['.f-date', 'date'], ['.f-note', 'note']])
          q(cls).addEventListener('input', () => { S[key] = q(cls).value; render(); changed(); });
      }
    }
    q('.f-mine').onchange = () => { S.mine = q('.f-mine').value; render(); changed(); };
    let armed = null;
    q('.f-clear').onclick = () => {
      const res = tapRes(), b = q('.f-clear'), label = `${resultWord}をすべて消す`;
      if (!Object.keys(res).length) return;
      if (!armed) { b.textContent = 'もう一度押すと消えます'; armed = setTimeout(() => { armed = null; b.textContent = label; }, 3000); return; }
      clearTimeout(armed); armed = null; b.textContent = label;
      for (const k of Object.keys(res)) delete res[k];
      render(); changed();
    };

    const fmt = p => p >= 0.9995 ? '100%' : p < 0.0005 ? (p > 0 ? '<0.1%' : '–') : (p * 100).toFixed(p < 0.1 ? 1 : 0) + '%';

    function render() {
      entries = parse(S.text, findTeam);
      const built = build(entries);
      root = built.root;

      // 表記の確認（入力できるときだけ）
      if (editable) {
        const unknown = entries.filter(e => !e.bye && !e.team);
        let wh = '';
        for (const e of unknown) {
          const cand = (opt.ctx.allNames || []).map(([n, to]) => [sim(e.input, n), to]).sort((a, b) => b[0] - a[0]);
          const seen = new Set(), top = cand.filter(c => c[0] >= 0.34 && !seen.has(c[1]) && seen.add(c[1])).slice(0, 3);
          wh += `<div>「<b>${esc(e.input)}</b>」（${e.ln + 1}行目）は登録されていません。${top.length ? 'もしかして：' + top.map(c =>
            `<button data-ln="${e.ln}" data-to="${esc(c[1])}">${esc(c[1])}</button>`).join('') : ''}<span class="meta">（このままなら初出場校 1500 として計算）</span></div>`;
        }
        const names = entries.filter(e => !e.bye).map(e => e.name), dup = names.filter((n, i) => names.indexOf(n) !== i);
        if (dup.length) wh += `<div>同じ学校が2回入っています：${[...new Set(dup)].map(esc).join('、')}</div>`;
        for (const w of built.warns) wh += `<div>${esc(w)}</div>`;
        q('.o-warn').innerHTML = wh ? `<div class="warn">${wh}</div>` : '';
        q('.o-warn').querySelectorAll('button[data-ln]').forEach(b => b.onclick = () => {
          const lines = q('.f-text').value.split(/\r?\n/), ln = +b.dataset.ln, e = entries.find(x => x.ln === ln);
          lines[ln] = b.dataset.to + (e.wait ? ' *' + e.wait : '') + (e.wd ? ' 棄権' : '');
          q('.f-text').value = lines.join('\n'); S.text = q('.f-text').value; render(); changed();
        });
      } else {
        q('.o-head').innerHTML = `<h2 style="margin-top:0">${esc(S.event || '次の大会')}</h2>
          <div class="meta">${esc([S.date, S.note].filter(Boolean).join(' ／ '))}</div>
          <p class="meta" style="margin-bottom:0">組み合わせは管理者が公開したものです。勝つと思う学校をタップすると、あなたの予想としてこの端末に保存されます（ほかの人には見えません）。</p>`;
      }

      const ok = root && !root.leaf;
      for (const c of ['.o-prob', '.o-tree']) q(c).hidden = !ok;
      if (!ok) { q('.o-review').hidden = true; return; }

      // 組み合わせが変わって使えなくなった入力を掃除
      const keys = new Set(); walk(root, n => { if (!n.leaf) keys.add(n.key); });
      for (const res of [S.results, S.picks]) for (const k of Object.keys(res)) if (!keys.has(k)) delete res[k];
      // 古い保存形式 {side} を {w:学校名} に直す
      walk(root, n => { if (n.leaf) return; for (const res of [S.results, S.picks]) { const r = res[n.key];
        if (r && !r.w && r.side) { const A = decided(n.a, res), B = decided(n.b, res); if (A && B && A !== BYE && B !== BYE) r.w = r.side === 'a' ? A : B; delete r.side; if (!r.w) delete res[n.key]; } } });

      // ---- 確率 ----
      const PR = probRes();
      dist(root, PR);
      const realTeams = entries.filter(e => !e.bye && !e.wd);
      let maxDepth = 0; walk(root, (n, d) => { if (n.leaf) maxDepth = Math.max(maxDepth, d); });
      const nSt = Math.min(STAGES.length, maxDepth);
      const reach = {};
      walk(root, (n, d, path) => {
        if (!n.leaf || n.e.bye || n.e.wd) return;
        const nm = n.e.name, full = [...path, n], row = [];
        for (let k = 0; k < nSt; k++) {
          const node = full[k];
          row.push(!node || node.leaf ? 1 : (node._d ? node._d[nm] || 0 : (decided(node, PR) === nm ? 1 : 0)));
        }
        reach[nm] = row;
      });
      q('.o-phead').innerHTML = `<tr><th>学校</th><th class="num">レート</th>${STAGES.slice(0, nSt).map(s => `<th class="num">${s}</th>`).join('')}</tr>`;
      const sorted = realTeams.map(e => e.name).filter(n => reach[n]).sort((a, b) => {
        for (let k = 0; k < nSt; k++) if (reach[b][k] !== reach[a][k]) return reach[b][k] - reach[a][k]; return 0; });
      const sel = q('.f-mine'), want = S.mine || opt.ctx.highlight || '';
      sel.innerHTML = '<option value="">（選ばない）</option>' + realTeams.map(e => e.name).sort((a, b) => a.localeCompare(b, 'ja')).map(n => `<option>${esc(n)}</option>`).join('');
      if (realTeams.some(e => e.name === want)) sel.value = want;
      const mine = sel.value;
      q('.o-pbody').innerHTML = sorted.map(n => `<tr class="${n === mine ? 'hl' : ''}"><td>${esc(n)}</td><td class="num rd">${findTeam(n) ? rOf(n).rating : '新'}</td>${reach[n].map(p => `<td class="num">${fmt(p)}</td>`).join('')}</tr>`).join('');
      q('.o-goal').innerHTML = mine && reach[mine] ? `<div class="goal">${esc(mine)}：${STAGES.slice(0, nSt).map((s, k) => `${s} <b>${fmt(reach[mine][k])}</b>`).reverse().join('　')}</div>` : '';
      q('.o-pnote').textContent = mode === 'next'
        ? '今のレーティングで、組み合わせどおりに戦った場合の確率です。大会中は、管理者が入れた実際の結果をもとに、残りの試合だけで計算し直します（あなたの予想は確率には使いません）。'
        : '今のレーティングで、組み合わせどおりに戦った場合の確率です。下で結果を入れると、残りの試合だけで計算し直します。';

      // ---- ブロック ----
      const bd = Math.max(1, Math.min(3, maxDepth - 2)), blocks = [];
      walk(root, (n, d) => { if (d === bd && !n.leaf) blocks.push(n); });
      const bl = blocks.map((n, i) => {
        const ts = []; walk(n, x => { if (x.leaf && !x.e.bye && !x.e.wd) ts.push(x.e.name); });
        const top3 = ts.slice().sort((a, b) => rOf(b).rating - rOf(a).rating).slice(0, 3);
        const avg = top3.reduce((s, x) => s + rOf(x).rating, 0) / Math.max(top3.length, 1);
        const fav = ts.slice().sort((a, b) => ((reach[b] || [])[bd] || 0) - ((reach[a] || [])[bd] || 0))[0];
        return { i, fav, p: reach[fav] ? reach[fav][bd] : 0, avg, top3 };
      });
      const hardest = bl.reduce((m, x) => x.avg > m ? x.avg : m, 0);
      q('.o-blocks').innerHTML = bl.map(b => { const h = b.avg === hardest && bl.length > 1;
        return `<tr class="${h ? 'death' : ''}"><td>${String.fromCharCode(65 + b.i)}${h ? ' <span class="upset">最激戦</span>' : ''}</td>
        <td>${esc(b.fav || '')}</td><td class="num">${fmt(b.p)}</td><td class="num">${Math.round(b.avg)}</td><td class="meta">${b.top3.map(esc).join('、')}</td></tr>`; }).join('');

      // ---- 組み合わせ（回戦ごと） ----
      const VR = viewRes(), TR = tapRes();
      const cols = {};
      walk(root, n => { if (!n.leaf) (cols[n.level] = cols[n.level] || []).push(n); });
      const levels = Object.keys(cols).map(Number).sort((a, b) => a - b), last = levels[levels.length - 1];
      const rname = lv => lv === last ? '決勝' : lv === last - 1 ? '準決勝' : lv === last - 2 ? '準々決勝' : lv + '回戦';
      for (const lv of levels) cols[lv].sort((a, b) => a.lo - b.lo);
      let hit = 0, judged = 0;
      q('.o-rounds').innerHTML = levels.map(lv => `<div class="rcol"><h3>${rname(lv)}</h3>${cols[lv].map((n, j) => {
        const dA = decided(n.a, VR), dB = decided(n.b, VR), Ab = dA === BYE, Bb = dB === BYE, wo = Ab || Bb;
        const A = Ab ? null : dA, B = Bb ? null : dB;
        const off = mode === 'next' ? official[n.key] : null;
        const offW = off ? winnerOf(off, A, B) : null;
        const r = off && offW ? off : VR[n.key];
        const w = winnerOf(r, A, B);
        const myPick = mode === 'next' ? winnerOf(S.picks[n.key], A, B) : null;
        let mark = '';
        if (mode === 'next' && offW && S.picks[n.key]) {
          const pw = S.picks[n.key].w; judged++;
          if (pw === offW) { hit++; mark = '<span class="hit">○的中</span>'; } else mark = '<span class="miss">×</span>';
        }
        const p = A && B ? winProb(rOf(A), rOf(B)) : null;
        const pl = (side, name, isBye, pp) => {
          if (isBye) return `<div class="pl tbd"><span>${n[side].leaf && n[side].e.wd ? esc(n[side].e.name) + '（棄権）' : 'bye'}</span><span></span></div>`;
          if (!name) return `<div class="pl tbd"><span>（未定）</span><span></span></div>`;
          let cls = '';
          if (wo) cls = 'win lock';
          else if (offW) cls = (offW === name ? 'win' : 'lose') + ' lock';
          else if (w) cls = (w === name ? (mode === 'next' ? 'pick' : 'win') : 'lose');
          const tap = !wo && !offW ? `data-k="${n.key}" data-n="${esc(name)}"` : '';
          return `<div class="pl ${cls}" ${tap}><span>${esc(name)}${mode === 'next' && myPick === name && offW ? ' <span class="tag">予想</span>' : ''}</span><span>${pp != null ? Math.round(pp * 100) + '%' : ''}</span></div>`;
        };
        const scoreBtn = !wo && !offW && w && mode !== 'next'
          ? `<button data-sc="${n.key}">${r.score === 'w' ? '棄権' : '3-' + String(r.score).slice(2)}</button>` : '';
        const offTxt = offW ? `<span class="tag">結果${off.score && off.score !== 'w' ? ' 3-' + String(off.score).slice(2) : off.score === 'w' ? ' 棄権' : ''}</span>` : '';
        return `<div class="mc ${wo ? 'wo' : ''}">${pl('a', A, Ab, p)}${pl('b', B, Bb, p != null ? 1 - p : null)}
          <div class="ft"><span>第${j + 1}試合${wo ? '・不戦' : ''}</span><span>${mark} ${offTxt}${scoreBtn}</span></div></div>`;
      }).join('')}</div>`).join('');
      q('.o-tnote').textContent = mode === 'next'
        ? '勝つと思う学校をタップします（もう一度タップで取り消し）。大会が始まると、管理者が入れた実際の結果（「結果」の印）が表示され、あなたの予想が当たったかどうかが分かります。'
        : mode === 'admin'
          ? '勝った学校をタップします（もう一度タップで取り消し）。スコアは下のボタンで 3-0 → 3-1 → 3-2 → 棄権 と切り替わります。入れた内容はこの端末に下書きとして保存されます。'
          : '勝った（勝つと思う）学校をタップします（もう一度タップで取り消し）。スコアは下のボタンで 3-0 → 3-1 → 3-2 → 棄権 と切り替わります。入力はこの端末に保存され、ほかの人には見えません。';
      q('.o-score').innerHTML = mode === 'next'
        ? `<div class="legend2"><span><i style="background:repeating-linear-gradient(135deg,var(--accent-soft) 0 6px,transparent 6px 10px)"></i>あなたの予想</span><span><i style="background:var(--accent-soft)"></i>実際の結果</span></div>${judged ? `<div class="goal">予想の的中：<b>${hit} / ${judged}</b> 試合</div>` : ''}`
        : '';
      q('.o-rounds').querySelectorAll('.pl[data-k]').forEach(d => d.onclick = () => {
        const k = d.dataset.k, nm = d.dataset.n, cur = TR[k];
        if (cur && cur.w === nm) delete TR[k];            // 同じ学校をもう一度タップ → 取り消し
        else TR[k] = { w: nm, score: cur && cur.score ? cur.score : '3-0' };
        // 勝ち上がる学校が変わって、上の回戦の入力が使えなくなったら消す
        // （「次の大会」の予想は、外れても答え合わせに使うので残す）
        if (mode !== 'next') for (let pass = 0; pass < 8; pass++) { let del = 0;
          walk(root, n => { if (!n.leaf && TR[n.key]) { const A = decided(n.a, TR), B = decided(n.b, TR);
            if (!A || !B || A === BYE || B === BYE || !winnerOf(TR[n.key], A, B)) { delete TR[n.key]; del++; } } });
          if (!del) break; }
        render(); changed();
      });
      q('.o-rounds').querySelectorAll('button[data-sc]').forEach(b => b.onclick = () => {
        const r = TR[b.dataset.sc], seq = ['3-0', '3-1', '3-2', 'w'];
        r.score = seq[(seq.indexOf(r.score) + 1) % seq.length]; render(); changed();
      });

      // ---- 振り返り（実際の結果がある試合で） ----
      const RR = mode === 'next' ? official : S.results;
      let total = 0, nRes = 0; const act = {}, perExp = {};
      walk(root, n => {
        if (n.leaf) return;
        const A = decided(n.a, RR), B = decided(n.b, RR), r = RR[n.key];
        if (A === BYE || B === BYE) return;
        const w = A && B ? winnerOf(r, A, B) : null;
        if (w && r.score === 'w') return;
        total++;
        if (w) { nRes++; act[w] = (act[w] || 0) + 1; const p = winProb(rOf(A), rOf(B)); perExp[A] = (perExp[A] || 0) + p; perExp[B] = (perExp[B] || 0) + 1 - p; }
      });
      const complete = nRes > 0 && nRes >= total;
      const exp = {}; if (complete) { dist(root, null, exp); dist(root, PR); }
      q('.o-review').hidden = !nRes || mode === 'free';
      if (nRes && mode !== 'free') {
        q('.o-rnote').textContent = complete
          ? '大会前の予想（この組み合わせで、ふつうなら何勝できたか）と、実際の勝利数の比較です。'
          : `途中経過です（${nRes}/${total}試合）。終わった試合それぞれの勝つ確率を足した値と、実際の勝利数を比べています。全試合が終わると、組み合わせ全体での比較に変わります。`;
        const base = complete ? exp : perExp;
        const rows = realTeams.map(e => e.name).filter(n => complete || perExp[n] != null)
          .map(n => ({ n, e: base[n] || 0, a: act[n] || 0 })).map(x => ({ ...x, d: x.a - x.e })).sort((a, b) => b.d - a.d);
        q('.o-rbody').innerHTML = rows.map(x => `<tr class="${x.n === mine ? 'hl' : ''}"><td>${esc(x.n)}</td><td class="num">${x.e.toFixed(1)}</td><td class="num">${x.a}</td>
          <td class="num ${x.d >= 0.05 ? 'up' : x.d <= -0.05 ? 'down' : ''}">${x.d > 0 ? '+' : ''}${x.d.toFixed(1)}</td></tr>`).join('');
      }
    }

    function getState() { return JSON.parse(JSON.stringify(S)); }

    /** 実際に行われた試合（不戦・棄権を除く）。管理者用ページで CSV を作るのに使う */
    function playedMatches() {
      const out = []; let open = 0;
      if (!root || root.leaf) return { list: out, open };
      const cols = {};
      walk(root, n => { if (!n.leaf) (cols[n.level] = cols[n.level] || []).push(n); });
      const levels = Object.keys(cols).map(Number).sort((a, b) => a - b), last = levels[levels.length - 1];
      const rname = lv => lv === last ? '決勝' : lv === last - 1 ? '準決勝' : lv === last - 2 ? '準々決勝' : lv + '回戦';
      for (const lv of levels) for (const n of cols[lv].sort((a, b) => a.lo - b.lo)) {
        const A = decided(n.a, S.results), B = decided(n.b, S.results);
        if (A === BYE || B === BYE) continue;
        const r = S.results[n.key], w = A && B ? winnerOf(r, A, B) : null;
        if (!w) { open++; continue; }
        if (r.score === 'w') continue;
        out.push({ round: rname(lv), w, l: w === A ? B : A, lose: +String(r.score).slice(2) });
      }
      return { list: out, open };
    }
    function unknownSchools() { return [...new Set(parse(S.text, findTeam).filter(e => !e.bye && !e.team).map(e => e.name))]; }
    function setOfficial(o) { official = (o && o.results) || {}; render(); }

    render();
    return { getState, playedMatches, unknownSchools, setOfficial, render };
  };
})();
