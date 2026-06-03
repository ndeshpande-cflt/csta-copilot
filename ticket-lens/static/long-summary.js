// Dashboard-style renderer for the long summary.
// Parses Claude's markdown output (### sections) and emits typed cards.

(function (window) {
  const SECTION_TYPES = [
    { match: /customer\s*org/i,            type: 'org' },
    { match: /customer\s*personnel/i,      type: 'personnel' },
    { match: /executive\s*summary/i,       type: 'exec' },
    { match: /ticket\s*metadata/i,         type: 'metadata' },
    { match: /interaction\s*timeline/i,    type: 'timeline' },
    { match: /resolution.*pending/i,       type: 'kv' },
    { match: /customer\s*sentiment/i,      type: 'sentiment' },
    { match: /support\s*team\s*evaluation/i, type: 'kv' },
    { match: /internal\s*message/i,        type: 'internal' },
    { match: /action\s*items/i,            type: 'actions' },
  ];

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function inline(s) {
    return escapeHtml(s)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  }
  function cleanHeading(h) {
    return h.replace(/^[^A-Za-z0-9]+/, '').trim();
  }
  function sectionType(heading) {
    for (const s of SECTION_TYPES) if (s.match.test(heading)) return s.type;
    return 'prose';
  }

  function splitSections(raw) {
    const out = [];
    let current = null;
    for (const line of raw.replace(/\r\n?/g, '\n').split('\n')) {
      const m = /^###\s+(.+?)\s*$/.exec(line);
      if (m) {
        if (current) out.push(current);
        current = { heading: m[1], lines: [] };
      } else if (current) {
        current.lines.push(line);
      }
    }
    if (current) out.push(current);
    return out;
  }

  function parseBullets(lines) {
    return lines
      .filter(l => /^\s*[-*]\s+/.test(l))
      .map(l => l.replace(/^\s*[-*]\s+/, ''));
  }
  function parseBulletKVs(lines) {
    const kvs = [];
    for (const bullet of parseBullets(lines)) {
      const m = /^(?:\*\*)?([^:*][^:]*?)(?:\*\*)?:\s*(.+)$/.exec(bullet);
      if (m) kvs.push({ key: m[1].trim(), value: m[2].trim() });
    }
    return kvs;
  }
  function parseTable(lines) {
    const rows = lines.filter(l => l.trim().startsWith('|')).map(l => l.trim());
    if (rows.length < 2) return null;
    const split = r => r.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
    const header = split(rows[0]);
    // Skip the |---|---| separator row.
    const sepIdx = rows.findIndex(r => /^\|\s*:?-+/.test(r));
    const body = rows.slice(sepIdx >= 0 ? sepIdx + 1 : 1).map(split);
    return { header, body };
  }

  function card(label, contentHtml, extraClass = '') {
    return `<section class="dcard ${extraClass}">
      <div class="dcard-label">${escapeHtml(label)}</div>
      ${contentHtml}
    </section>`;
  }

  function renderOrg(label, lines) {
    const text = lines.join('\n').trim();
    return card(label, `<div class="dcard-org-name">${inline(text)}</div>`, 'dcard-hero');
  }

  function renderPersonnel(label, lines) {
    const items = parseBullets(lines);
    if (!items.length) return card(label, `<div class="dcard-prose muted">—</div>`);
    const chips = items.map(i => {
      // Strip "(email)" from display but keep on hover
      const m = /^(.+?)\s*[(<]([^)>]+)[)>]\s*$/.exec(i);
      if (m) return `<span class="contact-item" title="${escapeHtml(m[2])}">${inline(m[1])}</span>`;
      return `<span class="contact-item">${inline(i)}</span>`;
    }).join('');
    return card(label, `<div class="dcard-chips">${chips}</div>`);
  }

  function renderMetadata(label, lines) {
    const kvs = parseBulletKVs(lines);
    if (!kvs.length) return renderProse(label, lines);
    const tiles = kvs.map(({ key, value }) => `
      <div class="stat-tile">
        <div class="stat-label">${inline(key)}</div>
        <div class="stat-value">${inline(value)}</div>
      </div>
    `).join('');
    return card(label, `<div class="dcard-stats">${tiles}</div>`);
  }

  function renderSentiment(label, lines) {
    const kvs = parseBulletKVs(lines);
    const sent = kvs.find(kv => /sentiment/i.test(kv.key));
    const reason = kvs.find(kv => /reasoning/i.test(kv.key));
    const val = sent ? sent.value : '—';
    const slug = val.toLowerCase().replace(/[^a-z]/g, '') || 'unknown';
    const reasonHtml = reason
      ? `<div class="dcard-prose">${inline(reason.value)}</div>`
      : '';
    return card(label,
      `<div class="sentiment-row">
        <span class="sentiment-pill sentiment-${slug}">${inline(val)}</span>
      </div>${reasonHtml}`);
  }

  function renderKV(label, lines) {
    const kvs = parseBulletKVs(lines);
    if (!kvs.length) return renderProse(label, lines);
    const rows = kvs.map(({ key, value }) => `
      <div class="kv-row">
        <div class="kv-key">${inline(key)}</div>
        <div class="kv-value">${inline(value)}</div>
      </div>
    `).join('');
    return card(label, `<div class="dcard-kv">${rows}</div>`);
  }

  function renderTimeline(label, lines) {
    const items = parseBullets(lines);
    if (!items.length) return renderProse(label, lines);
    const html = items.map(item => {
      const m = /^(\d{4}-\d{2}-\d{2}(?:[T\s]\d{1,2}:\d{2}(?::\d{2})?)?|\w+ \d{1,2},?\s*\d{4}(?:\s+\d{1,2}:\d{2}(?:\s?[AP]M)?)?)\s*[-—:]\s*(.+)$/.exec(item);
      if (m) {
        return `<li class="tl-item">
          <span class="tl-date">${inline(m[1])}</span>
          <span class="tl-text">${inline(m[2])}</span>
        </li>`;
      }
      return `<li class="tl-item"><span class="tl-text">${inline(item)}</span></li>`;
    }).join('');
    return card(label, `<ul class="tl">${html}</ul>`);
  }

  function renderActions(label, lines) {
    const tbl = parseTable(lines);
    if (!tbl) return renderProse(label, lines);
    const head = tbl.header.map(h => `<th>${inline(h)}</th>`).join('');
    const body = tbl.body.map(row => {
      const cells = row.map((c, i) => {
        const colName = (tbl.header[i] || '').toLowerCase();
        if (colName.includes('status')) {
          const slug = c.toLowerCase().replace(/[^a-z]/g, '') || 'unknown';
          return `<td><span class="action-status status-${slug}-pill">${inline(c)}</span></td>`;
        }
        if (colName.includes('owner')) {
          return `<td><span class="owner-pill">${inline(c)}</span></td>`;
        }
        return `<td>${inline(c)}</td>`;
      }).join('');
      return `<tr>${cells}</tr>`;
    }).join('');
    return card(label,
      `<div class="dcard-table-wrap"><table class="action-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table></div>`);
  }

  function renderInternal(label, lines) {
    const text = lines.join('\n').trim();
    if (!text) return '';
    return card(label, `<div class="dcard-prose">${inline(text).replace(/\n/g, '<br>')}</div>`, 'dcard-callout');
  }

  function renderProse(label, lines) {
    const text = lines.join('\n').trim();
    if (!text) return card(label, `<div class="dcard-prose muted">—</div>`);
    return card(label, `<div class="dcard-prose">${inline(text).replace(/\n/g, '<br>')}</div>`);
  }

  function renderSection({ heading, lines }) {
    const type = sectionType(heading);
    const label = cleanHeading(heading);
    // Trim trailing/leading blank lines.
    while (lines.length && !lines[0].trim()) lines.shift();
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    switch (type) {
      case 'org':        return renderOrg(label, lines);
      case 'personnel':  return renderPersonnel(label, lines);
      case 'metadata':   return renderMetadata(label, lines);
      case 'sentiment':  return renderSentiment(label, lines);
      case 'kv':         return renderKV(label, lines);
      case 'timeline':   return renderTimeline(label, lines);
      case 'actions':    return renderActions(label, lines);
      case 'internal':   return renderInternal(label, lines);
      default:           return renderProse(label, lines);
    }
  }

  window.renderLongSummary = function (raw) {
    if (!raw || !raw.trim()) return '';
    const sections = splitSections(raw);
    if (!sections.length) {
      // Fall back to the plain markdown renderer if there are no ### sections.
      return window.renderMarkdown ? window.renderMarkdown(raw) : `<p>${escapeHtml(raw)}</p>`;
    }
    return `<div class="dossier">${sections.map(renderSection).join('')}</div>`;
  };
})(window);
