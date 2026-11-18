<template>
  <section class="page" data-module="evaluate_board">
    <header class="page-head">
      <div><h2>评估看板</h2><p class="page-desc">维护看板配置的输入、执行约束与结果记录，为评估看板提供可验证的数据工程流程。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <!-- ===================== 消费范围聚合快照工作区 ===================== -->
    <div class="snapshot-panel" data-testid="snapshot-panel">
      <div class="snapshot-head">
        <div>
          <h3>消费范围聚合快照</h3>
          <p class="page-desc">按消费范围裁剪 AB 对比结果后聚合发布；越权实验不进指标卡，受控快照只读，V011 未登记口径卡片拒绝。</p>
        </div>
        <label class="operator-box"><span>操作成员</span>
          <select v-model="operator" data-testid="operator-select">
            <option v-for="m in operatorOptions" :key="m" :value="m">{{ m }}</option>
          </select>
        </label>
      </div>

      <p v-if="snapshotError" class="error-text" data-testid="snapshot-error">{{ snapshotError }}</p>
      <p v-if="snapshotMessage" class="ok-text" data-testid="snapshot-message">{{ snapshotMessage }}</p>

      <div class="snapshot-layout">
        <!-- 列表侧栏：与详情页共用 /boards 同一组数据 -->
        <aside class="board-sidebar" data-testid="board-sidebar">
          <div class="sidebar-title">看板列表（侧栏）</div>
          <button
            v-for="b in boards"
            :key="b.id"
            class="board-item"
            :class="{ active: b.id === selectedBoardId }"
            data-testid="board-item"
            @click="selectBoard(b.id)"
          >
            <strong>{{ b['看板编号'] }} · {{ b['看板名称'] }}</strong>
            <span class="board-meta">
              范围：{{ b['消费范围'] || '未绑定' }} ｜
              聚合版本：{{ b.snapshot ? 'v' + b.snapshot.snapshot_version : '—' }}
            </span>
            <span :class="b.snapshot && !b.snapshot.scope_consistent ? 'badge warn' : 'badge ok'">
              {{ !b.snapshot ? '无快照' : (b.snapshot.scope_consistent ? '范围一致' : '待移出') }}
            </span>
          </button>
        </aside>

        <!-- 详情：数据来自 /boards/{id}，与侧栏同源 -->
        <div class="board-detail" data-testid="board-detail" v-if="selectedBoard">
          <div class="detail-head">
            <h4>{{ selectedBoard['看板编号'] }} · {{ selectedBoard['看板名称'] }}</h4>
            <div class="detail-actions">
              <select v-model="bindScopeCode" data-testid="scope-select">
                <option v-for="s in scopes" :key="s.scope_code" :value="s.scope_code">
                  {{ s.scope_code }}（{{ s.scope_name }}）
                </option>
              </select>
              <button class="btn" data-testid="bind-scope" @click="bindScope">绑定消费范围</button>
              <button class="btn primary" data-testid="refresh-rebuild" @click="refreshSnapshot('rebuild')">全量重建刷新</button>
              <button class="btn" data-testid="refresh-incremental" @click="refreshSnapshot('incremental')">增量移出刷新</button>
              <button class="btn" data-testid="consumer-read" @click="consumerRead">消费端只读获取</button>
              <button class="btn" data-testid="v011-verify" @click="verifyV011">V011 核对</button>
              <button class="btn ghost" data-testid="show-archive" @click="loadArchive">V011 留档</button>
            </div>
          </div>

          <div class="scope-hint" v-if="selectedScope">
            获准成员（{{ selectedScope.member_count }}）：{{ selectedScope.members.join('、') }}
            ｜ 获准实验：{{ selectedScope.experiments.join('、') }}
          </div>

          <!-- 范围未收敛警告：增量策略下旧范围卡片暂留 -->
          <div v-if="snapshot && !snapshot.scope_consistent" class="warn-banner" data-testid="inconsistent-banner">
            当前快照保留旧范围聚合，存在待移出实验：
            <span v-for="p in snapshot.pending_removals" :key="p.experiment_code">
              {{ p.experiment_code }}（{{ p.metric_codes.join('、') }}）
            </span>
          </div>

          <!-- 聚合指标卡：成员数与卡片聚合值一致 -->
          <div class="stat-row" v-if="snapshot">
            <div class="stat-card"><span class="stat-label">指标卡数量</span><span class="stat-value">{{ snapshot.aggregate.card_count }}</span></div>
            <div class="stat-card"><span class="stat-label">获准实验数</span><span class="stat-value">{{ snapshot.aggregate.experiment_count }}</span></div>
            <div class="stat-card"><span class="stat-label">成员聚合值</span><span class="stat-value">{{ snapshot.aggregate.member_total }}</span></div>
            <div class="stat-card"><span class="stat-label">新范围收敛成员</span><span class="stat-value">{{ snapshot.aggregate.scoped_member_total }}</span></div>
            <div class="stat-card"><span class="stat-label">待移出成员</span><span class="stat-value">{{ snapshot.aggregate.pending_member_total }}</span></div>
            <div class="stat-card"><span class="stat-label">范围成员数</span><span class="stat-value">{{ snapshot.aggregate.scope_member_total }}</span></div>
            <div class="stat-card"><span class="stat-label">平均差异</span><span class="stat-value">{{ snapshot.aggregate.avg_diff }}</span></div>
            <div class="stat-card"><span class="stat-label">范围/聚合版本</span><span class="stat-value">{{ snapshot.scope_version }} / v{{ snapshot.snapshot_version }}</span></div>
          </div>

          <p v-else class="empty-state">该看板尚未发布聚合快照，请先绑定消费范围并刷新。</p>

          <template v-if="snapshot">
            <h5>指标卡（仅获准实验 + V011 已登记口径）</h5>
            <table class="data-table" data-testid="card-table">
              <thead><tr><th>实验</th><th>指标</th><th>口径版本</th><th>成员数</th><th>对照均值</th><th>实验均值</th><th>差异</th></tr></thead>
              <tbody>
                <tr v-for="c in snapshot.cards" :key="c.report_code">
                  <td>{{ c.experiment_code }} {{ c.experiment_name }}</td>
                  <td>{{ c.metric_code }} {{ c.metric_name }}</td>
                  <td>{{ c.metric_version }}</td>
                  <td>{{ c.member_count }}（{{ c.control_members }}+{{ c.treatment_members }}）</td>
                  <td>{{ c.control_mean }}</td>
                  <td>{{ c.treatment_mean }}</td>
                  <td>{{ c.diff }}</td>
                </tr>
              </tbody>
            </table>

            <h5>越权实验裁剪留档（不计入卡片）</h5>
            <table class="data-table" data-testid="excluded-table">
              <thead><tr><th>实验</th><th>关联报告</th><th>被裁剪成员数</th><th>原因</th></tr></thead>
              <tbody>
                <tr v-for="e in snapshot.excluded_experiments" :key="e.experiment_code">
                  <td>{{ e.experiment_code }} {{ e.experiment_name }}</td>
                  <td>{{ e.report_codes.join('、') }}</td>
                  <td>{{ e.member_count }}</td>
                  <td>{{ e.reason }}</td>
                </tr>
                <tr v-if="!snapshot.excluded_experiments.length"><td colspan="4" class="empty-state">无越权实验</td></tr>
              </tbody>
            </table>

            <h5>V011 口径核对 / 拒绝明细</h5>
            <ul class="check-list" data-testid="v011-checks">
              <li v-for="chk in snapshot.v011_checks" :key="chk.report_code" :class="chk.registered ? 'pass' : 'reject'">
                {{ chk.report_code }} · {{ chk.metric_code }}/{{ chk.metric_version }} —— {{ chk.result }}
              </li>
            </ul>
          </template>

          <div v-if="v011Report" class="v011-report" data-testid="v011-report">
            <h5>V011 调用链核对结论：<span :class="v011Report.all_pass ? 'pass' : 'reject'">{{ v011Report.all_pass ? '全部通过' : '存在拒绝' }}</span></h5>
            <ul class="check-list">
              <li v-for="c in v011Report.checks" :key="c.experiment_code + c.metric_code" :class="c.registered ? 'pass' : 'reject'">
                {{ c.experiment_code }} · {{ c.metric_code }}/{{ c.metric_version }} —— {{ c.result }}
              </li>
            </ul>
          </div>

          <div v-if="archiveItems.length" class="archive-box" data-testid="archive-box">
            <h5>V011 留档核对（{{ archiveItems.length }} 条，含失败回滚记录）</h5>
            <ul class="check-list">
              <li v-for="(a, i) in archiveItems" :key="i" :class="a.status === 'published' ? 'pass' : 'reject'">
                v{{ a.snapshot_version }} · 范围版本 {{ a.scope_version }} · {{ a.strategy }} · {{ a.status }}
                <template v-if="a.fail_reason"> —— {{ a.fail_reason }}</template>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/evaluate_board'
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = ["看板编号", "看板名称", "关联实验", "展示指标", "数据范围", "刷新策略", "消费范围", "看板版本"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["看板编号", "看板名称", "关联实验", "展示指标", "数据范围", "刷新策略", "消费范围", "看板版本"].slice(0, 5))
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function runAnalysis() {
  loading.value = true
  try { const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) }); const data = await res.json(); results.value = data.items || []; chartData.value = (data.items || []).map((r: any) => ({ label: Object.values(r)[0] as string, value: Object.values(r).filter(v => typeof v === 'number')[0] as number || 0 })).slice(0, 6) } catch(e) { results.value = [] }
  loading.value = false
}

// ---------------- 消费范围聚合快照 ----------------
type Scope = { scope_code: string; scope_name: string; members: string[]; member_count: number; experiments: string[] }
type Snapshot = Record<string, any>
const scopes = ref<Scope[]>([])
const boards = ref<Record<string, any>[]>([])
const selectedBoardId = ref<number | null>(null)
const selectedBoard = ref<Record<string, any> | null>(null)
const snapshot = ref<Snapshot | null>(null)
const bindScopeCode = ref('SCOPE_A')
const operator = ref('u001')
const operatorOptions = ['u001', 'u002', 'u003', 'u004', 'u005']
const snapshotError = ref('')
const snapshotMessage = ref('')
const v011Report = ref<Record<string, any> | null>(null)
const archiveItems = ref<Record<string, any>[]>([])

const selectedScope = computed<Scope | undefined>(() =>
  scopes.value.find(s => s.scope_code === (selectedBoard.value?.['消费范围'] || bindScopeCode.value)))

function flashError(detail: string) {
  snapshotError.value = detail
  snapshotMessage.value = ''
}
function flashMessage(msg: string) {
  snapshotMessage.value = msg
  snapshotError.value = ''
}

async function parseError(res: Response): Promise<string> {
  try { const data = await res.json(); return data.detail || `请求失败（${res.status}）` } catch { return `请求失败（${res.status}）` }
}

async function loadScopes() {
  const res = await request(`${ENDPOINT}/scopes`)
  scopes.value = (await res.json()).items || []
}

async function loadBoards(selectId?: number) {
  const res = await request(`${ENDPOINT}/boards`)
  boards.value = (await res.json()).items || []
  if (boards.value.length) {
    const target = selectId ?? selectedBoardId.value ?? boards.value[0].id
    await selectBoard(target)
  }
}

async function selectBoard(id: number) {
  selectedBoardId.value = id
  v011Report.value = null
  archiveItems.value = []
  snapshotError.value = ''
  snapshotMessage.value = ''
  const res = await request(`${ENDPOINT}/boards/${id}`)
  const board = await res.json()
  selectedBoard.value = board
  snapshot.value = board.snapshot
  if (board['消费范围']) bindScopeCode.value = board['消费范围']
}

async function bindScope() {
  if (!selectedBoardId.value) return
  const res = await request(`${ENDPOINT}/boards/${selectedBoardId.value}/scope`, {
    method: 'POST',
    body: JSON.stringify({ scope_code: bindScopeCode.value, operator: operator.value }),
  })
  if (!res.ok) return flashError(await parseError(res))
  flashMessage('消费范围已绑定，范围版本与后续聚合版本将同步发布')
  await loadBoards(selectedBoardId.value)
}

async function refreshSnapshot(strategy: 'rebuild' | 'incremental') {
  if (!selectedBoardId.value) return
  const res = await request(`${ENDPOINT}/boards/${selectedBoardId.value}/snapshot/refresh`, {
    method: 'POST',
    body: JSON.stringify({ operator: operator.value, strategy }),
  })
  if (!res.ok) {
    const detail = await parseError(res)
    flashError(detail)
    // 409/422 后刷新详情，确认上一份获准范围快照仍在
    await selectBoard(selectedBoardId.value)
    return
  }
  snapshot.value = await res.json()
  flashMessage(strategy === 'rebuild' ? '已按新消费范围全量重建发布' : '已按增量策略发布，旧范围卡片暂留并附待移除清单')
  await loadBoards(selectedBoardId.value)
}

async function consumerRead() {
  if (!selectedBoardId.value) return
  const res = await request(
    `${ENDPOINT}/boards/${selectedBoardId.value}/snapshot?operator=${encodeURIComponent(operator.value)}`)
  if (!res.ok) return flashError(await parseError(res))
  snapshot.value = await res.json()
  flashMessage('消费端已取得全量获准范围快照（只读，受控内容未被修改）')
}

async function verifyV011() {
  if (!selectedBoardId.value) return
  const res = await request(`${ENDPOINT}/boards/${selectedBoardId.value}/v011/verify`, {
    method: 'POST',
    body: JSON.stringify({ operator: operator.value }),
  })
  if (!res.ok) return flashError(await parseError(res))
  v011Report.value = await res.json()
}

async function loadArchive() {
  if (!selectedBoardId.value) return
  const res = await request(
    `${ENDPOINT}/boards/${selectedBoardId.value}/archive?operator=${encodeURIComponent(operator.value)}`)
  if (!res.ok) return flashError(await parseError(res))
  archiveItems.value = (await res.json()).items || []
}

onMounted(() => {
  fetchSummary()
  loadScopes().then(() => loadBoards()).catch(e => flashError(String(e)))
})
</script>
<style scoped>
.snapshot-panel { margin-top: 24px; background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.snapshot-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.snapshot-head h3 { margin: 0 0 4px; }
.operator-box span { display: block; font-size: 12px; color: var(--muted); }
.operator-box select { padding: 6px; border: 1px solid var(--border); border-radius: 6px; }
.snapshot-layout { display: grid; grid-template-columns: 280px 1fr; gap: 14px; margin-top: 12px; }
.board-sidebar { border: 1px solid var(--border); border-radius: 8px; padding: 8px; background: #fafbfc; display: flex; flex-direction: column; gap: 8px; }
.sidebar-title { font-size: 13px; color: var(--muted); font-weight: 600; }
.board-item { text-align: left; border: 1px solid var(--border); border-radius: 8px; background: #fff; padding: 8px 10px; cursor: pointer; display: flex; flex-direction: column; gap: 4px; }
.board-item.active { border-color: var(--brand); box-shadow: 0 0 0 1px var(--brand); }
.board-meta { font-size: 12px; color: var(--muted); }
.badge { align-self: flex-start; font-size: 11px; padding: 2px 8px; border-radius: 999px; }
.badge.ok { background: #dcfae6; color: #15803d; }
.badge.warn { background: #fef3c7; color: #b45309; }
.board-detail { border: 1px solid var(--border); border-radius: 8px; padding: 12px; min-width: 0; }
.detail-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.detail-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.detail-actions select { padding: 6px; border: 1px solid var(--border); border-radius: 6px; }
.scope-hint { font-size: 12px; color: var(--muted); background: #f8fafc; border-radius: 6px; padding: 8px; margin: 8px 0; }
.warn-banner { background: #fffbeb; border: 1px solid #fcd34d; color: #92400e; border-radius: 8px; padding: 8px 10px; font-size: 13px; margin: 8px 0; }
.warn-banner span { margin-right: 10px; font-weight: 600; }
.ok-text { color: #15803d; font-size: 13px; }
h5 { margin: 14px 0 6px; font-size: 13px; color: #334155; }
.check-list { list-style: none; padding: 0; margin: 0; font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
.check-list .pass { color: #15803d; }
.check-list .reject { color: #b42318; }
.archive-box { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 8px; }
@media (max-width: 960px) { .snapshot-layout { grid-template-columns: 1fr; } }
</style>
