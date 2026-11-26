<template>
  <section class="dispatch-page" data-testid="incident-dispatch-page">
    <header class="page-head">
      <div>
        <h2>网关事故补采派工</h2>
        <p class="page-desc">
          从网关事故号进入事故明细，按「实际网关 + 设备身份」圈定现存缺口；派工与原单点补采共享同一份签收证据和确认读数。
        </p>
      </div>
      <div class="page-actions dispatch-toolbar">
        <label class="incident-search">
          网关事故号
          <input
            v-model="incidentId"
            type="text"
            list="incident-options"
            data-testid="incident-no-input"
            placeholder="输入网关事故号，如 INC-G1-1400"
            @keyup.enter="loadAll"
          />
          <datalist id="incident-options">
            <option v-for="item in incidents" :key="item.id" :value="item['网关事故号'] || item['事故编号']">
              {{ item['网关编号'] }}
            </option>
          </datalist>
        </label>
        <button class="btn" type="button" data-testid="query-incident" @click="loadAll">查询事故明细</button>
        <button class="btn" type="button" data-testid="refresh-workbench" @click="loadAll">刷新工作台</button>
        <button class="btn primary" type="button" data-testid="claim-selected" :disabled="submitting || selected.size === 0" @click="claimSelected">
          领取选中并派工（{{ selected.size }}）
        </button>
      </div>
    </header>

    <div v-if="message" class="notice success" data-testid="dispatch-message">{{ message }}</div>
    <div v-if="errorMessage" class="notice error" data-testid="dispatch-error">{{ errorMessage }}</div>

    <section v-if="incident" class="incident-card" data-testid="incident-detail">
      <div>
        <h3>事故明细 · <span data-testid="incident-no">{{ incident['网关事故号'] || incident['事故编号'] }}</span></h3>
        <p><strong>网关事故号：</strong>{{ incident['网关事故号'] || incident['事故编号'] }}</p>
        <p><strong>实际网关：</strong><span data-testid="incident-gateway">{{ incident['网关编号'] }}</span></p>
        <p><strong>故障边界：</strong>{{ incident['故障开始'] || '14:00' }} – {{ incident['故障结束'] || '14:10' }}（含边界内缺失时刻）</p>
        <p><strong>圈定设备身份：</strong>{{ (incident['圈定设备身份'] || []).join('、') }}；设备名 D</p>
        <p class="isolation" data-testid="g2-isolation">
          邻站 G2 虽有同名设备 D（DEV-G2-D），但实际网关不一致：G2 不会列为本事故可派项，也没有被派工。
        </p>
      </div>
      <div class="uniqueness-card" data-testid="reading-uniqueness">
        <span>确认读数约束</span>
        <strong>每个时刻最终只产生一份确认读数</strong>
        <small data-testid="reading-count">当前确认读数 {{ workbench?.readings?.length ?? 0 }} 份；唯一性：{{ uniquenessText }}</small>
        <label class="receipt-value">
          迟到签收读数
          <input v-model="receiptValue" data-testid="late-receipt-value" type="text" />
        </label>
      </div>
    </section>

    <section class="table-card">
      <div class="section-title">
        <h3>事故时段的现存缺口</h3>
        <span>只展示实际属于 {{ incident?.['网关编号'] || 'G1' }} 且身份匹配的设备时刻</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th>
            <th>时刻</th>
            <th>实际网关</th>
            <th>设备身份</th>
            <th>设备</th>
            <th>断点</th>
            <th>工作台状态</th>
            <th>补采来源 / 完成证据</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in positions"
            :key="row.idempotency_key"
            :data-testid="`gap-row-${row['clock'] || row['时刻']}`"
            :data-state="row.dispatch_state"
          >
            <td>
              <input
                type="checkbox"
                :data-testid="`gap-select-${row['clock'] || row['时刻']}`"
                :checked="selected.has(String(row['记录编号']))"
                @change="toggleSelected(String(row['记录编号']))"
              />
            </td>
            <td><strong data-testid="gap-time">{{ row['clock'] || row['时刻'] }}</strong></td>
            <td>{{ row['网关编号'] }}</td>
            <td>{{ row['设备身份'] }}</td>
            <td>{{ row['设备名称'] || row['设备编号'] }}</td>
            <td>{{ row['记录编号'] }}</td>
            <td>
              <span class="state-badge" :data-state="row.dispatch_state" :data-testid="`state-${row['clock'] || row['时刻']}`">
                {{ row.state_label }}
              </span>
            </td>
            <td class="source-cell" data-testid="source-evidence">
              <div v-if="row.shared_request_id">申请：{{ row.shared_request_id }}</div>
              <div v-if="row.evidence_id">证据：{{ row.evidence_id }}</div>
              <div v-if="row.reading_id">读数：{{ row.reading_id }}</div>
              <div v-if="!row.shared_request_id && !row.evidence_id">尚无出站申请</div>
            </td>
            <td class="row-actions wrap">
              <button
                class="link"
                type="button"
                :data-testid="`claim-${row['clock'] || row['时刻']}`"
                @click="claimOne(String(row['记录编号']))"
              >
                {{ row.dispatch_state === 'not_sent' ? '领取 / 继续执行' : '领取 / 续跑核对' }}
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`single-${row['clock'] || row['时刻']}`"
                @click="startOriginal(String(row['记录编号']))"
              >
                原单点补采已发出
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`receipt-${row['clock'] || row['时刻']}`"
                @click="receiveLateReceipt(row)"
              >
                录入迟到签收
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="workbench-grid" data-testid="state-workbench">
      <article class="state-column not-sent">
        <h4>确实未发（{{ groups.not_sent.length }}）</h4>
        <p v-for="item in groups.not_sent" :key="item.idempotency_key" :data-testid="`wb-not-sent-${item['时刻']}`">
          {{ item['时刻'] }} · {{ item['设备身份'] }}
        </p>
        <p v-if="!groups.not_sent.length" class="muted">无</p>
      </article>
      <article class="state-column outbound">
        <h4>已出站但仍需等候确认（{{ groups.outbound_pending.length }}）</h4>
        <p v-for="item in groups.outbound_pending" :key="item.idempotency_key" :data-testid="`wb-pending-${item['时刻']}`">
          {{ item['时刻'] }} · {{ item.shared_request_id || item.request_id }}
        </p>
        <p v-if="!groups.outbound_pending.length" class="muted">无</p>
      </article>
      <article class="state-column completed">
        <h4>已经由原申请完成（{{ groups.completed_by_original.length }}）</h4>
        <p v-for="item in groups.completed_by_original" :key="item.idempotency_key" :data-testid="`wb-original-${item['时刻']}`">
          {{ item['时刻'] }} · 证据 {{ item.evidence_id }}
        </p>
        <p v-if="!groups.completed_by_original.length" class="muted">无</p>
      </article>
    </section>

    <section v-if="orderDetail" class="order-card" data-testid="dispatch-detail">
      <div class="section-title">
        <h3>补采派工单 {{ orderDetail['派工单编号'] }}</h3>
        <div>
          <button class="btn" type="button" data-testid="refresh-detail" @click="refreshOrder()">刷新派工详情</button>
          <button class="btn primary" type="button" data-testid="close-order" @click="closeOrder">核对并收尾</button>
        </div>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>时刻</th>
            <th>状态</th>
            <th>采用的补采来源</th>
            <th>同份完成证据 / 读数</th>
            <th>另一笔与收尾</th>
            <th>核对</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in orderDetail.items" :key="item.id" :data-testid="`order-item-${item['时刻']}`">
            <td>{{ item['时刻'] }}</td>
            <td><span class="state-badge" :data-state="item.dispatch_state">{{ item.state_label }}</span></td>
            <td data-testid="adopted-source">
              <div>{{ sourceText(item) }}</div>
              <small v-if="item.original_request_id">原申请：{{ item.original_request_id }}</small>
            </td>
            <td data-testid="shared-evidence-detail">
              <div v-if="item.evidence_id">证据：{{ item.evidence_id }}</div>
              <div v-if="item.reading_id">确认读数：{{ item.reading_id }}</div>
              <div v-if="item.shared_evidence">两张单可查同一份完成证据</div>
            </td>
            <td data-testid="other-closeout">
              <div>{{ closeoutText(item) }}</div>
              <small v-if="orderDetail.closeout?.other_order_ids?.length">
                另一派工单：{{ orderDetail.closeout.other_order_ids.join('、') }}
              </small>
              <small v-if="orderDetail.closeout?.other_request_ids?.length">
                关联申请：{{ orderDetail.closeout.other_request_ids.join('、') }}
              </small>
            </td>
            <td>
              <button
                v-if="item.dispatch_state === 'not_sent' || item.dispatch_state === 'outbound_pending'"
                class="link"
                type="button"
                :data-testid="`execute-order-item-${item['时刻']}`"
                @click="executeOrderItem(item)"
              >
                继续执行
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`verify-${item['时刻']}`"
                @click="verifyItem(item)"
              >
                核对补采来源
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div class="evidence-panel" data-testid="evidence-panel">
        <h4>完成证据</h4>
        <pre>{{ JSON.stringify(orderDetail.evidence || [], null, 2) }}</pre>
        <h4>确认读数（同一时刻仅一条）</h4>
        <pre data-testid="readings-json">{{ JSON.stringify(orderDetail.readings || [], null, 2) }}</pre>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, any>
type Workbench = Row
type OrderDetail = Row

const DEFAULT_INCIDENT_ID = 'INC-G1-1400'
const route = useRoute()
const queryIncident = route.query.incident_id || route.query.incidentId || route.query.accidentNo || route.query.no

const incidentId = ref(queryIncident ? String(queryIncident) : DEFAULT_INCIDENT_ID)
const incidents = ref<Row[]>([])
const workbench = ref<Workbench | null>(null)
const orderDetail = ref<OrderDetail | null>(null)
const currentOrderId = ref<string>('')
const selected = ref<Set<string>>(new Set())
const receiptValue = ref('24.6')
const message = ref('')
const errorMessage = ref('')
const submitting = ref(false)

const incident = computed<Row | null>(() => workbench.value?.incident ?? null)
const positions = computed<Row[]>(() => workbench.value?.positions ?? [])
const groups = computed(() => ({
  not_sent: (workbench.value?.not_sent ?? []) as Row[],
  outbound_pending: (workbench.value?.outbound_pending ?? []) as Row[],
  completed_by_original: (workbench.value?.completed_by_original ?? []) as Row[],
}))
const uniquenessText = computed(() => {
  const audit = workbench.value?.reading_uniqueness
  if (!audit) return '未审计'
  return audit.unique ? `通过（${audit.moments} 个时刻）` : '发现重复读数'
})

async function postJson(path: string, body: Row): Promise<Row> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const result = (await response.json()) as Row
  if (!response.ok) throw new Error(result.message || `接口返回 ${response.status}`)
  return result
}

function notify(result: Row, successFallback: string) {
  if (result.ok === false) {
    errorMessage.value = result.message || '操作未完成'
    message.value = ''
  } else {
    message.value = result.message || successFallback
    errorMessage.value = ''
    const orderId = result.order?.['派工单编号'] || result.dispatch_order?.['派工单编号']
    if (orderId) currentOrderId.value = String(orderId)
  }
}

async function loadAll() {
  errorMessage.value = ''
  try {
    const response = await request(`/api/incident-dispatch/workbench?incident_id=${encodeURIComponent(incidentId.value)}`)
    workbench.value = (await response.json()) as Workbench
    if (currentOrderId.value) await refreshOrder(false)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '工作台读取失败'
  }
}

async function refreshOrder(reloadWorkbench = true) {
  if (!currentOrderId.value) return
  try {
    const response = await request(`/api/incident-dispatch/orders/${currentOrderId.value}`)
    orderDetail.value = (await response.json()) as OrderDetail
    if (reloadWorkbench) await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派工详情读取失败'
  }
}

function toggleSelected(gapId: string) {
  const next = new Set(selected.value)
  if (next.has(gapId)) next.delete(gapId)
  else next.add(gapId)
  selected.value = next
}

async function claimSelected() {
  submitting.value = true
  try {
    const result = await postJson('/api/incident-dispatch/dispatch', {
      incident_id: incidentId.value,
      gap_ids: Array.from(selected.value),
      order_id: currentOrderId.value || undefined,
    })
    notify(result, '已领取并派发选中断点')
    selected.value = new Set()
    await refreshOrder()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派工失败'
  } finally {
    submitting.value = false
  }
}

async function claimOne(gapId: string) {
  submitting.value = true
  try {
    const result = await postJson('/api/incident-dispatch/dispatch', {
      incident_id: incidentId.value,
      gap_ids: [gapId],
      order_id: currentOrderId.value || undefined,
    })
    notify(result, '已领取并执行')
    await refreshOrder()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
  } finally {
    submitting.value = false
  }
}

async function startOriginal(gapId: string) {
  try {
    const result = await postJson('/api/incident-dispatch/single-recovery', {
      gap_id: gapId,
      source_type: 'single_application',
    })
    notify(result, '原单点补采已发出')
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '原申请发起失败'
  }
}

async function receiveLateReceipt(row: Row) {
  try {
    const requestId = row.shared_request_id || row.request_id || row.original_request_id
    const result = await postJson('/api/incident-dispatch/receipts', {
      request_id: requestId || undefined,
      gap_id: row['记录编号'],
      gateway_id: row['网关编号'],
      device_id: row['设备身份'],
      sample_time: row['时刻'],
      value: receiptValue.value,
    })
    notify(result, '迟到签收已处理')
    if (result.dispatch_detail?.order_ids?.[0]) {
      currentOrderId.value = String(result.dispatch_detail.order_ids[0])
    }
    await refreshOrder()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '签收失败'
  }
}

async function executeOrderItem(item: Row) {
  if (!currentOrderId.value) return
  try {
    const result = await postJson(`/api/incident-dispatch/orders/${currentOrderId.value}/execute`, {
      item_id: item['明细编号'] || item.id,
      gap_id: item.gap_id,
    })
    notify(result, '已继续执行；同点申请不会重复出站')
    await refreshOrder()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '继续执行失败'
  }
}

async function verifyItem(item: Row) {
  if (!currentOrderId.value) return
  try {
    const result = await postJson(`/api/incident-dispatch/orders/${currentOrderId.value}/verify`, {
      item_id: item['明细编号'] || item.id,
    })
    notify(result, '补采来源已核对')
    await refreshOrder()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对失败'
  }
}

async function closeOrder() {
  if (!currentOrderId.value) return
  try {
    const response = await request(`/api/incident-dispatch/orders/${currentOrderId.value}/close`, {
      method: 'POST',
    })
    const result = (await response.json()) as Row
    notify(result, '派工单已收尾')
    await refreshOrder()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '收尾失败'
  }
}

function sourceText(item: Row): string {
  if (item.completion_source === 'original_application' || item.original_request_id) {
    return `采用原单点申请来源${item.original_request_id ? `（${item.original_request_id}）` : ''}，派工单未重复采样`
  }
  if (item.source_request_id) return `派工出站申请：${item.source_request_id}`
  return '尚未实际发出补采'
}

function closeoutText(item: Row): string {
  if (item.dispatch_state === 'completed_by_original') {
    return '原申请迟到签收已形成共享证据；另一笔可直接核对，无需再发起采样。'
  }
  if (item.dispatch_state === 'outbound_pending') {
    return '申请已出站，仍需等候签收确认；不要创建第二份采样结果。'
  }
  if (item.dispatch_state === 'not_sent') return '确实未发，可继续执行。'
  return item.closeout_status || '可核对是否仍需收尾。'
}

onMounted(async () => {
  try {
    const response = await request('/api/incident-dispatch/incidents')
    const payload = (await response.json()) as Row
    incidents.value = payload.items ?? []
  } catch {
    // 事故号候选列表失败不阻塞默认事故工作台。
  }
  await loadAll()
})
</script>

<style scoped>
.dispatch-page { display: flex; flex-direction: column; gap: 14px; }
.dispatch-toolbar { align-items: flex-end; flex-wrap: wrap; justify-content: flex-end; }
.incident-search { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.incident-search input { width: 260px; padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.notice { padding: 9px 12px; border-radius: 8px; font-size: 13px; }
.notice.success { background: #ecfdf3; border: 1px solid #abefc6; color: #067647; }
.notice.error { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; }
.incident-card, .table-card, .order-card, .state-column { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
.incident-card { display: flex; justify-content: space-between; gap: 16px; }
.incident-card h3, .order-card h3 { margin: 0 0 10px; }
.incident-card p { margin: 5px 0; font-size: 13px; }
.isolation { color: #b54708; background: #fffaeb; border: 1px solid #fedf89; border-radius: 6px; padding: 8px; }
.uniqueness-card { min-width: 280px; display: flex; flex-direction: column; gap: 6px; background: #f8fafc; border-radius: 8px; padding: 12px; font-size: 13px; }
.uniqueness-card small { color: var(--muted); }
.receipt-value { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.receipt-value input { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; }
.section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.section-title span { color: var(--muted); font-size: 12px; }
.row-actions.wrap { flex-wrap: wrap; gap: 8px; }
.state-badge { display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; background: #eef2f6; color: #475467; white-space: nowrap; }
.state-badge[data-state='not_sent'] { background: #f2f4f7; color: #344054; }
.state-badge[data-state='outbound_pending'] { background: #fffaeb; color: #b54708; }
.state-badge[data-state='completed_by_original'], .state-badge[data-state='completed'] { background: #ecfdf3; color: #067647; }
.source-cell { font-size: 12px; line-height: 1.5; }
.workbench-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.state-column h4 { margin: 0 0 10px; font-size: 14px; }
.state-column p { font-size: 13px; margin: 6px 0; }
.state-column.not-sent { border-top: 3px solid #98a2b3; }
.state-column.outbound { border-top: 3px solid #f79009; }
.state-column.completed { border-top: 3px solid #12b76a; }
.muted { color: var(--muted); }
.evidence-panel { margin-top: 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.evidence-panel pre { background: #101828; color: #d0d5dd; padding: 10px; border-radius: 8px; font-size: 11px; max-height: 240px; overflow: auto; margin: 0; }
.evidence-panel h4 { margin: 0 0 6px; font-size: 13px; }
@media (max-width: 1100px) {
  .incident-card, .workbench-grid, .evidence-panel { grid-template-columns: 1fr; display: grid; }
}
</style>
