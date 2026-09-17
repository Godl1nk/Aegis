// Shared, lazy tool picker for the structured edit and Add Skill forms.
let catalogPromise = null;

function loadCatalog() {
  if (!catalogPromise) {
    catalogPromise = fetch('/api/skills/tool-options', { credentials: 'same-origin' })
      .then(async response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!Array.isArray(data.tools)) throw new Error('Invalid tool catalogue');
        return data.tools;
      }).catch(error => { catalogPromise = null; throw error; });
  }
  return catalogPromise;
}

export function createRequiredToolPicker(initial = []) {
  const selected = new Set(initial.filter(name => typeof name === 'string'));
  const fieldset = document.createElement('fieldset');
  fieldset.className = 'skill-tool-picker';
  fieldset.innerHTML = '<legend>Required tools</legend>' +
    '<p class="memory-desc">Tick only tools this skill needs. Missing or disabled requirements can hide the skill. This does not grant permissions or enable tools.</p>' +
    '<details><summary></summary><input type="search" class="memory-search-input" aria-label="Filter required tools" placeholder="Find a tool…">' +
    '<div class="skill-tool-options"></div></details><p class="memory-desc skill-tool-status" role="status"></p>';
  const summary = fieldset.querySelector('summary');
  const search = fieldset.querySelector('input');
  const list = fieldset.querySelector('.skill-tool-options');
  const status = fieldset.querySelector('.skill-tool-status');
  let tools = [];
  function updateSummary() {
    summary.textContent = selected.size ? `${selected.size} selected: ${[...selected].join(', ')}` : 'Choose tools (none required)';
  }
  function render() {
    list.replaceChildren();
    const byName = new Map(tools.map(tool => [tool.name, tool]));
    // Preserve unknown/imported requirements, even if loading the catalog fails.
    for (const name of selected) if (!byName.has(name)) byName.set(name, { name, missing: true });
    const query = search.value.trim().toLowerCase();
    const entries = [...byName.values()].sort((a, b) => Number(selected.has(b.name)) - Number(selected.has(a.name)) || a.name.localeCompare(b.name));
    for (const tool of entries) {
      if (!`${tool.name} ${tool.description || ''}`.toLowerCase().includes(query)) continue;
      const label = document.createElement('label');
      label.className = 'skill-tool-option';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = tool.name;
      checkbox.checked = selected.has(tool.name);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(tool.name); else selected.delete(tool.name);
        updateSummary();
      });
      const text = document.createElement('span');
      text.textContent = tool.name + (tool.missing ? ' (not in catalogue)' : tool.unavailable ? ' (disabled or restricted)' : '');
      label.title = tool.description || tool.name;
      label.append(checkbox, text);
      list.appendChild(label);
    }
    if (!list.childElementCount) list.textContent = 'No matching tools.';
  }
  fieldset.getSelectedTools = () => [...selected].sort();
  fieldset.resetSelection = () => { selected.clear(); updateSummary(); render(); };
  fieldset.addEventListener('click', event => event.stopPropagation());
  search.addEventListener('input', render);
  updateSummary();
  render();
  const details = fieldset.querySelector('details');
  let loaded = false;
  let pending = false;
  details.addEventListener('toggle', async () => {
    if (!details.open || loaded || pending) return;
    pending = true;
    status.textContent = 'Loading tool catalogue…';
    try {
      tools = await loadCatalog();
      loaded = true;
      status.textContent = '';
      render();
    } catch {
      status.textContent = 'Could not load tools. Existing selections are preserved. Close and reopen to retry.';
    } finally { pending = false; }
  });
  return fieldset;
}
