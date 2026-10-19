<template>
  <div class="result-panel" v-if="hasResult || sqlText">
    <!-- 工具栏：视图切换 + 导出 -->
    <div v-if="hasResult" class="panel-toolbar">
      <el-radio-group v-model="viewMode" size="small">
        <el-radio-button value="table">表格</el-radio-button>
        <el-radio-button value="bar" :disabled="!chartable">柱状图</el-radio-button>
        <el-radio-button value="line" :disabled="!chartable">折线图</el-radio-button>
        <el-radio-button value="pie" :disabled="!chartable">饼图</el-radio-button>
      </el-radio-group>

      <div class="export-actions">
        <el-button size="small" @click="exportCSV">导出 CSV</el-button>
        <el-button size="small" type="primary" @click="exportExcel">导出 Excel</el-button>
      </div>
    </div>

    <!-- 数据为空兜底 -->
    <div v-if="!hasResult" class="empty-result">
      <el-empty description="查询结果为空" :image-size="80" />
    </div>

    <!-- 表格视图 -->
    <div v-else-if="viewMode === 'table'" class="table-view">
      <el-table
        :data="pagedRows"
        size="small"
        border
        stripe
        height="360"
        style="width: 100%"
      >
        <el-table-column
          v-for="col in columns"
          :key="col"
          :prop="col"
          :label="col"
          sortable
          min-width="120"
          show-overflow-tooltip
        />
      </el-table>
      <div class="pagination-wrap">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="rows.length"
          layout="total, sizes, prev, pager, next, jumper"
          background
          small
        />
      </div>
    </div>

    <!-- 图表视图 -->
    <div v-else class="chart-view">
      <div ref="chartEl" class="chart-canvas" />
    </div>

    <!-- SQL 展示 + 复制 -->
    <div v-if="sqlText" class="sql-block">
      <div class="sql-header">
        <span class="sql-label">生成 SQL</span>
        <el-button link type="primary" size="small" @click="copySql">
          {{ copied ? '已复制' : '复制' }}
        </el-button>
      </div>
      <pre class="sql-pre">{{ sqlText }}</pre>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

// echarts / xlsx 按需动态加载，避免把它们打进首页主包
let echartsP = null
function loadEcharts() {
  if (!echartsP) echartsP = import('echarts')
  return echartsP
}
let xlsxBuf = null
function loadXLSX() {
  if (!xlsxBuf) xlsxBuf = import('xlsx')
  return xlsxBuf
}

const props = defineProps({
  result: { type: Array, default: null },
  sqlText: { type: String, default: '' },
})

const rows = computed(() => (Array.isArray(props.result) ? props.result : []))
const hasResult = computed(() => rows.value.length > 0)
const columns = computed(() => {
  if (!rows.value.length) return []
  const set = new Set()
  rows.value.forEach((r) => Object.keys(r || {}).forEach((k) => set.add(k)))
  return [...set]
})

// ---------- 列类型分析 ----------
function isNumericValue(v) {
  if (v === null || v === undefined || v === '' || typeof v === 'boolean') return false
  return typeof v === 'number' || !isNaN(Number(v))
}

const analysis = computed(() => {
  if (!hasResult.value) return { dims: [], nums: [] }
  const sample = rows.value.slice(0, 50)
  const nums = []
  const dims = []
  columns.value.forEach((col) => {
    const values = sample.map((r) => r[col]).filter((v) => v !== null && v !== undefined && v !== '')
    if (!values.length) {
      dims.push(col)
      return
    }
    if (values.every(isNumericValue)) nums.push(col)
    else dims.push(col)
  })
  return { dims, nums }
})

const chartable = computed(() => {
  const { dims, nums } = analysis.value
  return dims.length >= 1 && nums.length >= 1
})

// 维度唯一值过多（>20）时默认不画图，仅展示表格
const defaultViewMode = computed(() => {
  const { dims, nums } = analysis.value
  if (!chartable.value) return 'table'
  const dim0Unique = new Set(rows.value.map((r) => r[dims[0]])).size
  if (dim0Unique > 20) return 'table'
  return 'bar'
})

const viewMode = ref(defaultViewMode.value)
watch(
  () => props.result,
  () => {
    viewMode.value = defaultViewMode.value
    nextTick(renderChart)
  },
)

// ---------- 表格分页 ----------
const currentPage = ref(1)
const pageSize = ref(10)
const pagedRows = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return rows.value.slice(start, start + pageSize.value)
})
watch(
  () => props.result,
  () => {
    currentPage.value = 1
  },
)

// ---------- ECharts ----------
const chartEl = ref(null)
let chartInstance = null

function getChartOption() {
  const { dims, nums } = analysis.value
  const measure = nums[0]
  const dim0 = dims[0]
  const categories = [...new Set(rows.value.map((r) => r[dim0]))]
  const mode = viewMode.value

  // 饼图：仅取第一个数值列
  if (mode === 'pie') {
    const data = categories.map((cat) => {
      const row = rows.value.find((r) => r[dim0] === cat)
      return { name: String(cat), value: Number(row?.[measure] ?? 0) }
    })
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      legend: { type: 'scroll', bottom: 0 },
      series: [{ type: 'pie', radius: ['35%', '65%'], center: ['50%', '45%'], data, label: { formatter: '{b}: {d}%' } }],
    }
  }

  // 柱状 / 折线
  // 若有 2+ 维度，用第二维度做分组系列；否则每个数值列一个系列
  let series
  if (dims.length >= 2) {
    const dim1 = dims[1]
    const groups = [...new Set(rows.value.map((r) => r[dim1]))]
    series = groups.map((g) => ({
      name: String(g),
      type: mode,
      data: categories.map((cat) => {
        const row = rows.value.find((r) => r[dim0] === cat && r[dim1] === g)
        return Number(row?.[measure] ?? 0)
      }),
    }))
  } else {
    series = nums.map((n) => ({
      name: n,
      type: mode,
      data: categories.map((cat) => {
        const row = rows.value.find((r) => r[dim0] === cat)
        return Number(row?.[n] ?? 0)
      }),
    }))
  }

  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, type: 'scroll' },
    grid: { left: 50, right: 20, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: categories, axisLabel: { interval: 0, rotate: categories.length > 8 ? 30 : 0 } },
    yAxis: { type: 'value' },
    series,
  }
}

async function renderChart() {
  if (!chartEl.value) return
  if (viewMode.value === 'table') {
    chartInstance?.dispose()
    chartInstance = null
    return
  }
  const echarts = await loadEcharts()
  if (!chartInstance) {
    chartInstance = echarts.init(chartEl.value)
  }
  chartInstance.setOption(getChartOption(), true)
}

function resizeChart() {
  chartInstance?.resize()
}

watch(viewMode, () => nextTick(renderChart))

let resizeObserver = null
onMounted(() => {
  nextTick(renderChart)
  window.addEventListener('resize', resizeChart)
  // 容器尺寸变化时自适应
  if (typeof ResizeObserver !== 'undefined' && chartEl.value) {
    resizeObserver = new ResizeObserver(resizeChart)
    resizeObserver.observe(chartEl.value)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  resizeObserver?.disconnect()
  chartInstance?.dispose()
  chartInstance = null
})

// ---------- 复制 SQL ----------
const copied = ref(false)
async function copySql() {
  const text = props.sqlText || ''
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      // 兜底：临时 textarea + execCommand
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copied.value = true
    ElMessage.success('SQL 已复制到剪贴板')
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    ElMessage.error('复制失败，请手动选择复制')
  }
}

// ---------- 导出 ----------
function timestamp() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function exportCSV() {
  if (!hasResult.value) return
  const cols = columns.value
  const esc = (v) => {
    const s = v === null || v === undefined ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [cols.map(esc).join(',')]
  rows.value.forEach((r) => lines.push(cols.map((c) => esc(r[c])).join(',')))
  // 加 UTF-8 BOM，避免 Excel 打开中文乱码
  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
  downloadBlob(blob, `query_result_${timestamp()}.csv`)
  ElMessage.success('CSV 已导出')
}

async function exportExcel() {
  if (!hasResult.value) return
  const XLSX = await loadXLSX()
  const ws = XLSX.utils.json_to_sheet(rows.value, { header: columns.value })
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, 'QueryResult')
  XLSX.writeFile(wb, `query_result_${timestamp()}.xlsx`)
  ElMessage.success('Excel 已导出')
}
</script>

<style scoped>
.result-panel {
  margin-top: 10px;
}

.panel-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  gap: 10px;
  flex-wrap: wrap;
}

.export-actions {
  display: flex;
  gap: 8px;
}

.table-view {
  width: 100%;
}

.pagination-wrap {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

.chart-view {
  width: 100%;
}

.chart-canvas {
  width: 100%;
  height: 360px;
}

.empty-result {
  padding: 12px 0;
}

/* SQL 区块 */
.sql-block {
  margin-top: 12px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  overflow: hidden;
}

.sql-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  background: #f5f7fa;
  border-bottom: 1px solid #ebeef5;
}

.sql-label {
  font-size: 12px;
  color: #909399;
  font-weight: 600;
}

.sql-pre {
  margin: 0;
  padding: 10px 12px;
  background: #fafafa;
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
}
</style>
