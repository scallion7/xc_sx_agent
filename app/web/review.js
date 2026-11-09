import {
  RequestClient
} from './request_client.js';
const $ = id => document.getElementById(id);
const client = new RequestClient(() => $('reviewToken').value);
let project = null,
  selected = null,
  evidence = [],
  generation = 0;
const labels = {
  pending: '待复核',
  draft: '草稿',
  approved: '已批准',
  disputed: '有分歧'
};
const base = () => '/v1/admin/evaluation/reviews/' + project.project_id;
const casePath = () => base() + '/cases/' + selected.case.id;
const node = (tag, text, cls = '') => {
  const n = document.createElement(tag);
  n.textContent = text;
  n.className = cls;
  return n;
};

function status(text, error = false) {
  $('reviewStatus').textContent = text;
  $('reviewStatus').classList.toggle('error', error);
}

function handle(fn) {
  return async () => {
    try {
      await fn();
    } catch (e) {
      status(e.message, true);
    }
  };
}

function controls() {
  for (const id of ['saveDraft', 'approveReview', 'disputeReview', 'loadHistory', 'diagnose']) $(id).disabled = !selected;
  for (const id of ['exportReview', 'importReview']) $(id).disabled = !project;
}

function renderCases() {
  $('progress').textContent = `已批准 ${project.approved_count} / ${project.cases.length} 题 · 数据版本 ${project.manifest.version} · 修订 ${project.revision}`;
  $('cases').replaceChildren();
  for (const c of project.cases) {
    if ($('splitFilter').value !== 'all' && c.split !== $('splitFilter').value) continue;
    const b = node('button', `${c.id} · ${labels[c.status]}\n${c.query}`, c.id === selected?.case.id ? 'active' : 'secondary');
    b.onclick = handle(() => selectCase(c.id));
    $('cases').append(b);
  }
}

function renderEvidence() {
  $('evidence').replaceChildren();
  for (const [i, c] of evidence.entries()) {
    const p = node('div', `${c.document_id} [${c.start}, ${c.end})\n${c.quote}`, 'card quote');
    const b = node('button', '移除', 'secondary');
    b.onclick = () => {
      evidence.splice(i, 1);
      renderEvidence();
    };
    p.append(b);
    $('evidence').append(p);
  }
}

function renderDetail(data) {
  selected = data;
  evidence = structuredClone(data.annotation?.evidence || []);
  $('question').textContent = data.case.query;
  $('conditions').textContent = `${data.case.id} · ${data.case.split==='holdout'?'保留集':'开发集'} · 日期 ${data.case.day} · 条件 ${JSON.stringify(data.case.context)}`;
  $('suggestion').open = false;
  $('suggested').textContent = JSON.stringify(data.suggested, null, 2);
  $('outcome').value = data.annotation?.outcome || 'answerable';
  $('notes').value = data.annotation?.notes || '';
  $('reviewIdentity').textContent = data.actor ? `最近修改账号：${data.actor} · 修订 ${data.revision} · ${labels[data.annotation.status]}` : '尚无人工审核记录';
  $('history').replaceChildren();
  $('diagnostics').replaceChildren();
  $('documents').replaceChildren();
  for (const d of data.documents) {
    const card = node('article', '', 'card');
    card.append(node('h3', d.metadata.title), node('p', d.text, 'quote'), node('p', JSON.stringify(d.metadata), 'muted'));
    const b = node('button', '采用此政策');
    b.onclick = () => {
      if (!evidence.some(x => x.document_id === d.document_id)) {
        evidence.push({
          document_id: d.document_id,
          start: 0,
          end: Array.from(d.text).length,
          quote: d.text
        });
        renderEvidence();
      }
    };
    card.append(b);
    $('documents').append(card);
  }
  renderEvidence();
  renderCases();
  controls();
}
async function selectCase(id) {
  const epoch = ++generation;
  const data = await client.request(base() + '/cases/' + id);
  if (epoch === generation) renderDetail(data);
}
$('openReview').onclick = handle(async () => {
  const epoch = ++generation;
  const id = $('projectId').value.trim();
  const data = id ? await client.request('/v1/admin/evaluation/reviews/' + encodeURIComponent(id)) : await client.request('/v1/admin/evaluation/reviews', 'POST');
  if (epoch !== generation) return;
  project = data;
  selected = null;
  renderCases();
  controls();
  status('数据集已打开，请独立核对原文后填写结论。');
});
$('splitFilter').onchange = () => project && renderCases();
async function save(state) {
  const epoch = generation;
  const path = casePath();
  const projectPath = base();
  const a = {
    status: state,
    outcome: $('outcome').value,
    notes: $('notes').value,
    gold_documents: [...new Set(evidence.map(e => e.document_id))],
    evidence
  };
  for (const id of ['saveDraft', 'approveReview', 'disputeReview']) $(id).disabled = true;
  try {
    const data = await client.request(path, 'PUT', {
      expected_revision: selected.revision,
      annotation: a
    });
    if (epoch !== generation) return;
    const listing = await client.request(projectPath);
    if (epoch !== generation) return;
    project = listing;
    renderDetail(data);
    status(data.annotation.first_approval && data.annotation.status !== 'approved' ? '首位复核已记录：此题需要另一账号对相同内容独立确认。' : '已保存：' + labels[data.annotation.status] + '。审核账号与历史已记录。');
  } finally {
    controls();
  }
}
$('saveDraft').onclick = handle(() => save('draft'));
$('approveReview').onclick = handle(() => save('approved'));
$('disputeReview').onclick = handle(() => save('disputed'));
$('loadHistory').onclick = handle(async () => {
  const epoch = generation;
  const data = await client.request(casePath() + '/history');
  if (epoch !== generation) return;
  $('history').replaceChildren();
  for (const r of data.events) $('history').append(node('p', `修订 ${r.revision} · ${r.actor} · ${labels[r.annotation.status]} · ${r.annotation.notes}`, 'card'));
});
$('exportReview').onclick = handle(async () => {
  const epoch = generation;
  const data = await client.request(base() + '/export');
  if (epoch !== generation) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {
    type: 'application/json'
  }));
  const a = document.createElement('a');
  a.href = url;
  a.download = 'review-' + data.project_revision + '.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  status('已导出复核文件：' + (data.status === 'human_reviewed' ? '全部已批准' : '仍有题目待审核'));
});
$('importReview').onclick = handle(async () => {
  const file = $('importFile').files[0];
  if (!file) throw new Error('请选择复核 JSON 文件');
  if (file.size > 1000000) throw new Error('文件不能超过 1MB');
  const epoch = ++generation;
  const path = base();
  const revision = project.revision;
  const document = JSON.parse(await file.text());
  if (epoch !== generation) return;
  const result = await client.request(path + '/import', 'POST', {
    expected_revision: revision,
    document
  });
  if (epoch !== generation) return;
  project = result;
  selected = null;
  await selectCase(project.cases[0].id);
  status('已导入为草稿，原批准状态不继承。请重新逐题核对。');
});
$('diagnose').onclick = handle(async () => {
  const epoch = generation;
  $('diagnose').disabled = true;
  status('正在运行离线诊断…');
  try {
    const data = await client.request(casePath() + '/diagnostics', 'POST');
    if (epoch !== generation) return;
    $('diagnostics').replaceChildren();
    for (const [mode, result] of Object.entries(data.cases[0].modes)) {
      const box = node('details');
      box.open = true;
      box.append(node('summary', `${mode} · ${result.outcome} · ${result.results.length} 条结果`));
      box.append(node('p', '查询词：' + result.diagnostics.query_tokens.join(' / ') + '；词法重叠下限：' + (result.diagnostics.minimum_lexical_overlap ?? '不适用'), 'muted'));
      const table = document.createElement('table');
      const tr = document.createElement('tr');
      for (const h of ['政策', '过滤 / 条件', 'BM25 / 向量分数', '词法 / 向量名次', '最终名次', '选择原因']) tr.append(node('th', h));
      table.append(tr);
      for (const c of result.diagnostics.candidates) {
        const row = document.createElement('tr');
        for (const value of [c.document_id, c.filter_reasons.join(', ') || c.applicability, `${c.bm25_score?.toFixed(4)??'-'} / ${c.vector_score?.toFixed(4)??'-'}`, `${c.bm25_rank??'-'} / ${c.vector_rank??'-'}`, c.final_rank ?? '-', c.selection_reason]) row.append(node('td', String(value)));
        table.append(row);
      }
      const scroll = node('div', '', 'table-scroll');
      scroll.append(table);
      box.append(scroll);
      $('diagnostics').append(box);
    }
    status('诊断已完成；仅为离线工程诊断，不能证明真实语义质量。');
  } finally {
    controls();
  }
});
$('reviewToken').oninput = () => {
  generation++;
  project = null;
  selected = null;
  evidence = [];
  for (const id of ['cases', 'documents', 'evidence', 'diagnostics', 'history', 'suggested', 'candidateList', 'datasetList']) $(id).replaceChildren();
  $('question').textContent = '选择一道题目';
  $('notes').value = '';
  $('conditions').textContent = '';
  $('reviewIdentity').textContent = '';
  $('progress').textContent = '身份已切换，请重新打开数据集。';
  controls();
};

async function candidates() {
  const epoch = generation;
  const data = await client.request('/v1/admin/evaluation/candidates');
  if (epoch !== generation) return;
  $('candidateList').replaceChildren();
  for (const row of data.candidates) {
    const box = node('article', '', 'card');
    box.append(node('p', `${row.candidate_id} · ${row.classification} · ${row.status} · 修订 ${row.revision}`));
    const query = node('textarea'); query.value = row.query; query.setAttribute('aria-label', '脱敏案例问题');
    const notes = node('textarea'); notes.value = row.notes; notes.placeholder = '记录脱敏检查、问题复现与整理说明'; notes.setAttribute('aria-label', '候选整理说明');
    const save = node('button', '保存整理结果');
    save.onclick = handle(async () => {
      if (epoch !== generation) return;
      const result = await client.request('/v1/admin/evaluation/candidates/' + row.candidate_id, 'PUT', {expected_revision: row.revision, query: query.value, notes: notes.value});
      if (epoch !== generation) return;
      await candidates();
      status('已整理候选，修订 ' + result.revision + '；尚未成为人工金标准。');
    });
    box.append(query, notes, save); $('candidateList').append(box);
  }
}
$('loadCandidates').onclick = handle(candidates);
$('captureCandidate').onclick = handle(async () => {
  const epoch = generation;
  const row = await client.request('/v1/admin/evaluation/candidates', 'POST', {run_id: $('captureRun').value.trim(), classification: $('captureClass').value});
  if (epoch !== generation) return;
  await candidates(); status('已收集脱敏候选：' + row.candidate_id);
});
$('freezeDataset').onclick = handle(async () => {
  const epoch = generation;
  const file = $('datasetFile').files[0];
  if (!file || file.size > 4000000) throw new Error('请选择不超过 4MB 的数据集 JSON');
  const document = JSON.parse(await file.text());
  if (epoch !== generation) return;
  const result = await client.request('/v1/admin/evaluation/datasets', 'POST', document);
  if (epoch !== generation) return;
  $('projectId').value = result.project_id;
  status('新版本已冻结，所有题目待审核。点击打开冻结数据集开始复核。');
});
$('listDatasets').onclick = handle(async () => {
  const epoch = generation;
  const data = await client.request('/v1/admin/evaluation/datasets');
  if (epoch !== generation) return;
  $('datasetList').replaceChildren();
  for (const row of data.datasets) {
    const b = node('button', row.name + ' / ' + row.version, 'secondary');
    b.onclick = () => { $('projectId').value = row.project_id; $('openReview').click(); };
    $('datasetList').append(b);
  }
});
