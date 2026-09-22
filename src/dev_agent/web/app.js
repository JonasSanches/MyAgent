const $ = (selector) => document.querySelector(selector);
const messages = $('#messages');
const dialog = $('#action-dialog');
const loginDialog = $('#login-dialog');

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Algo não funcionou.');
  return data;
}
function escapeHtml(value) { const area = document.createElement('textarea'); area.textContent = value; return area.innerHTML; }
function addMessage(text, role = 'agent', code = false) {
  const article = document.createElement('article'); article.className = `message ${role}`;
  article.innerHTML = `<span class="avatar">${role === 'agent' ? '✦' : 'Você'}</span><div>${code ? `<pre>${escapeHtml(text)}</pre>` : `<p>${escapeHtml(text)}</p>`}</div>`;
  messages.append(article); messages.scrollTop = messages.scrollHeight; return article;
}
function addSolution(data) {
  const message = addMessage(`${data.message}\n\n${data.solution}`, 'agent');
  message.querySelector('div').insertAdjacentHTML('beforeend', `<div class="solution-actions"><button class="secondary">Testar esta solução</button></div>`);
  message.querySelector('button').onclick = () => testDialog(data.attempt_id);
}
function confirmation(data, prompt, images) {
  const message = addMessage(`${data.message}\n${data.detail}`, 'agent');
  message.querySelector('div').insertAdjacentHTML('beforeend', `<div class="solution-actions"><button class="primary">Consultar próxima camada</button><button class="secondary">Agora não</button></div>`);
  const [yes, no] = message.querySelectorAll('button');
  yes.onclick = async () => { yes.disabled = true; try { addSolution(await api('/api/chat', {method:'POST', body: JSON.stringify({message: prompt, images, authorize_codex: true})})); refresh(); } catch (error) { addMessage(error.message); } };
  no.onclick = () => { no.disabled = true; addMessage('Certo. Registrei a lacuna no histórico e não usei créditos.'); };
}
function testDialog(attemptId) {
  $('#dialog-content').innerHTML = `<h2>Verificar solução</h2><p>O teste informado por você será executado localmente. Se passar, mostro o que pode ser aprendido e peço aprovação.</p><label>Comando de teste<input id="test-command" value="python3 -m unittest"></label><label>Diretório do projeto<input id="workdir" value="."></label><div class="actions"><button class="primary" id="run-test">Executar teste</button><button class="secondary" id="close-dialog">Cancelar</button></div>`;
  dialog.showModal(); $('#close-dialog').onclick = () => dialog.close();
  $('#run-test').onclick = async () => { try { const result = await api('/api/complete', {method:'POST', body: JSON.stringify({attempt_id:attemptId,test_command:$('#test-command').value,workdir:$('#workdir').value})}); dialog.close(); testResult(result); refresh(); } catch(error) { addMessage(error.message); } };
}
function testResult(result) {
  if (!result.passed) { addMessage(`O teste falhou. A tentativa ficou registrada, mas não será aprendida.\n\n${result.output}`); return; }
  if (!result.requires_approval) { addMessage('Teste aprovado. A solução já era conhecimento permanente; aumentei sua confiança.'); return; }
  const message = addMessage(`Teste aprovado. Eis o que eu aprendi:\n\n${result.summary}`);
  message.querySelector('div').insertAdjacentHTML('beforeend', `<div class="solution-actions"><button class="primary">Aprovar conhecimento</button><button class="secondary">Manter só no histórico</button></div>`);
  const [approve, reject] = message.querySelectorAll('button');
  approve.onclick = async () => { try { const data = await api('/api/approve',{method:'POST',body:JSON.stringify({attempt_id:result.attempt_id})}); addMessage(data.message); refresh(); } catch(error) { addMessage(error.message); } };
  reject.onclick = async () => { try { const data = await api('/api/reject',{method:'POST',body:JSON.stringify({attempt_id:result.attempt_id})}); addMessage(data.message); refresh(); } catch(error) { addMessage(error.message); } };
}
async function refresh() {
  const state = await api('/api/state');
  $('#usage').textContent = `${state.usage.requests}/${state.usage.request_limit} consultas · ${state.usage.tokens}/${state.usage.token_budget} tokens`;
  $('#knowledge').innerHTML = state.knowledge.length ? state.knowledge.map(item => `<div class="list-item">${escapeHtml(item.title)}<small>${item.success_count} validação(ões)</small></div>`).join('') : '<p class="muted">Nenhum conhecimento permanente.</p>';
  $('#history').innerHTML = state.history.length ? state.history.map(item => `<div class="list-item">${escapeHtml(item.prompt)}<small>${item.source} · ${item.status}</small></div>`).join('') : '<p class="muted">Nenhuma tentativa ainda.</p>';
}
async function startSession() {
  try {
    const session = await api('/api/session');
    if (session.required && !session.authenticated) {
      loginDialog.showModal();
      return;
    }
    $('#logout').hidden = !session.required;
    await refresh();
  } catch (error) { addMessage(error.message); }
}
$('#login-form').onsubmit = async (event) => {
  event.preventDefault();
  $('#login-error').textContent = '';
  try {
    await api('/api/login', {method:'POST', body:JSON.stringify({password:$('#login-password').value})});
    $('#login-password').value = ''; loginDialog.close(); $('#logout').hidden = false; await refresh();
  } catch (error) { $('#login-error').textContent = error.message; }
};
$('#logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  $('#logout').hidden = true; loginDialog.showModal();
};
let selectedImages = [];
function renderAttachments() {
  $('#attachments').innerHTML = selectedImages.map((image, index) => `<span class="attachment">Print ${index + 1}<button type="button" data-index="${index}" aria-label="Remover print">×</button></span>`).join('');
  document.querySelectorAll('.attachment button').forEach(button => button.onclick = () => { selectedImages.splice(Number(button.dataset.index), 1); renderAttachments(); });
}
function readImage(file) {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
}
$('#images').onchange = async (event) => {
  const files = [...event.target.files]; event.target.value = '';
  if (selectedImages.length + files.length > 4 || files.some(file => file.size > 5 * 1024 * 1024)) { addMessage('Use no máximo 4 prints PNG, JPEG ou WebP de até 5 MB.'); return; }
  try { selectedImages.push(...await Promise.all(files.map(readImage))); renderAttachments(); } catch (_) { addMessage('Não consegui ler o print selecionado.'); }
};
$('#toggle-height').onclick = () => {
  const prompt = $('#prompt'); const expanded = prompt.classList.toggle('expanded');
  prompt.style.height = expanded ? '240px' : '64px';
  $('#toggle-height').textContent = expanded ? '↥' : '↕';
  $('#toggle-height').title = expanded ? 'Reduzir campo' : 'Aumentar campo';
};
$('#prompt').onkeydown = (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); } };
$('#composer').onsubmit = async (event) => {
  event.preventDefault(); const prompt = $('#prompt').value.trim(); const images = [...selectedImages]; if (!prompt && !images.length) return;
  addMessage(prompt || `[${images.length} print${images.length > 1 ? 's' : ''} anexado${images.length > 1 ? 's' : ''}]`, 'user'); $('#prompt').value = ''; selectedImages = []; renderAttachments();
  try { const data = await api('/api/chat',{method:'POST',body:JSON.stringify({message:prompt,images})}); data.kind === 'confirmation' ? confirmation(data,prompt,images) : addSolution(data); refresh(); } catch(error) { addMessage(error.message); }
};
$('#permissions').onclick = async () => { const state = await api('/api/state'); $('#dialog-content').innerHTML = `<h2>Permissões</h2>${state.permissions.map(item => `<p><strong>${escapeHtml(item.action)}</strong><br>${item.confirmation ? 'Pede sua confirmação.' : 'Atua com autonomia.'}</p>`).join('')}<div class="actions"><button class="secondary" id="close-dialog">Fechar</button></div>`; dialog.showModal(); $('#close-dialog').onclick=()=>dialog.close(); };
startSession();
