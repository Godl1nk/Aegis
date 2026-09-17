// One DOM message per agent response. Round nodes keep their identity so the
// streaming renderer can keep updating them without rebuilding the transcript.
function updateActivity(root, finished) {
  const activity = root.querySelector('.agent-turn-activity');
  const steps = root.querySelector('.agent-turn-steps');
  const tools = Array.from(steps.querySelectorAll('.agent-thread-node[data-tool]'));
  const running = tools.filter(node => node.classList.contains('running'));
  const failed = tools.filter(node => node.classList.contains('error'));
  const label = running.length ? (running[running.length - 1].dataset.tool || 'Working') : '';
  activity.querySelector('summary').textContent = `Activity${tools.length ? ` · ${tools.length} tool call${tools.length === 1 ? '' : 's'}` : ''}`
    + (failed.length ? ` · ${failed.length} failed` : '')
    + (label ? ` · ${label}…` : finished ? '' : ' · Working…');
  activity.hidden = !steps.childElementCount;
}

// Continue keeps the original message identity and both tool transcripts.
export function mergeAgentTurnActivity(target, source) {
  const incomingSteps = source.querySelector('.agent-turn-steps');
  if (!incomingSteps) return target;
  if (!target.classList.contains('agent-turn')) {
    const turn = createAgentTurn(target.parentNode, target);
    turn.finish({ raw: target.dataset.raw, dbId: target.dataset.dbId });
    target = turn.root;
  }
  target.querySelector('.agent-turn-steps').append(...incomingSteps.childNodes);
  updateActivity(target, true);
  return target;
}

export function createAgentTurn(parent, firstRound = null) {
  const root = document.createElement('div');
  root.className = 'msg msg-ai agent-turn';
  const role = document.createElement('div');
  role.className = 'role';
  const answer = document.createElement('div');
  answer.className = 'body agent-turn-answer';
  const activity = document.createElement('details');
  activity.className = 'agent-turn-activity';
  const summary = document.createElement('summary');
  summary.textContent = 'Activity';
  const steps = document.createElement('div');
  steps.className = 'agent-turn-steps';
  activity.append(summary, steps);
  activity.hidden = true;
  // Keep the answer first in DOM for existing copy/edit/variant actions;
  // CSS places activity before it visually.
  root.append(role, answer, activity);
  parent.insertBefore(root, firstRound || null);
  let currentRound = null;
  let finished = false;

  function sync(round = currentRound) {
    if (!round) return;
    const sourceRole = round.querySelector('.role');
    if (sourceRole) {
      const timestamp = role.querySelector('.role-timestamp')?.cloneNode(true);
      role.replaceChildren(...Array.from(sourceRole.childNodes, node => node.cloneNode(true)));
      role.style.cssText = sourceRole.style.cssText;
      role.title = sourceRole.title;
      if (timestamp && !role.querySelector('.role-timestamp')) role.append(timestamp);
    }
    if (round.dataset.dbId) root.dataset.dbId = round.dataset.dbId;
    if (round._memoriesUsed) root._memoriesUsed = round._memoriesUsed;
  }

  function update() {
    updateActivity(root, finished);
  }

  function archiveRound(round = currentRound) {
    if (!round || round.parentNode === steps) return;
    sync(round);
    steps.appendChild(round);
    if (round === currentRound) currentRound = null;
    update();
  }

  function appendRound(round) {
    if (currentRound && currentRound !== round) archiveRound();
    round.classList.remove('msg', 'msg-ai', 'msg-continuation');
    round.classList.add('agent-turn-round');
    answer.appendChild(round);
    currentRound = round;
    sync(round);
  }

  function finish({ raw, dbId, interrupted = false } = {}) {
    finished = true;
    sync();
    if (raw != null) root.dataset.raw = raw;
    if (dbId) root.dataset.dbId = dbId;
    root.classList.remove('streaming');
    root.querySelectorAll('.agent-turn-round.streaming').forEach(node => node.classList.remove('streaming'));
    // Stop/error paths can attach a footer to the original round. Keep one
    // visible footer and never let transcript steps count as extra messages.
    const footers = Array.from(root.querySelectorAll('.msg-footer'));
    const footer = footers.find(node => node.parentNode === root) || footers.pop();
    for (const node of footers) if (node !== footer) node.remove();
    if (footer) root.appendChild(footer);
    // Deliverables must remain discoverable without expanding the transcript.
    steps.querySelectorAll('.agent-doc-open-btn, .stopped-indicator').forEach(link => answer.appendChild(link));
    if (interrupted) activity.open = true;
    update();
  }

  if (firstRound) appendRound(firstRound);
  return { root, answer, activity, steps, appendRound, archiveRound, sync, update, finish };
}
