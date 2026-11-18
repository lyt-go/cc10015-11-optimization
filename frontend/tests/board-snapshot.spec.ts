import { expect, test } from '@playwright/test'

/**
 * 评估看板「消费范围聚合快照」工作区端到端验证。
 * 覆盖：范围裁剪、越权拒绝、版本同步、失败保留、V011 联动、冲突两策略、侧栏/详情同源。
 */
test.describe.serial('评估看板聚合快照', () => {
  test.beforeEach(async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('response', r => {
      if (r.url().includes('/api/evaluate_board/boards') && r.status() >= 400 && !r.url().includes('snapshot/refresh')) {
        errors.push(`${r.status()} ${r.url()}`)
      }
    })
    ;(page as any).__errors = errors
  })

  test('完整链路：裁剪→只读→越权→V011→冲突策略→留档', async ({ page, request }) => {
    await page.goto('/evaluate_board')

    // 工作区与侧栏渲染
    await expect(page.getByTestId('snapshot-panel')).toBeVisible()
    const boardItems = page.getByTestId('board-item')
    await expect(boardItems).toHaveCount(3)
    await expect(page.getByTestId('board-detail')).toBeVisible()

    // 1) 绑定 SCOPE_A 并全量重建刷新（默认操作成员 u001）
    await page.getByTestId('scope-select').selectOption('SCOPE_A')
    await page.getByTestId('bind-scope').click()
    await page.getByTestId('refresh-rebuild').click()

    // 指标卡只含获准实验，越权 EXPE-0003 进裁剪留档表
    const cardTable = page.getByTestId('card-table')
    await expect(cardTable).toBeVisible()
    const cardText = await cardTable.innerText()
    expect(cardText).toContain('EXPE-0001')
    expect(cardText).toContain('EXPE-0002')
    expect(cardText).not.toContain('EXPE-0003')

    const excludedText = await page.getByTestId('excluded-table').innerText()
    expect(excludedText).toContain('EXPE-0003')

    // 聚合版本与范围版本同步为 1，成员聚合值与卡片一致
    const statRow = page.locator('.stat-row').innerText()
    expect(statRow).toContain('1 / v1')
    const memberTotal = await page.locator('.stat-card').filter({ hasText: '成员聚合值' }).locator('.stat-value').innerText()
    const cardMemberCells = await cardTable.locator('tbody tr td:nth-child(4)').allInnerTexts()
    const sumFromCards = cardMemberCells.reduce((acc, cell) => acc + parseInt(cell, 10), 0)
    expect(parseInt(memberTotal, 10)).toBe(sumFromCards)

    // 侧栏与详情同源：侧栏第一项显示聚合版本 v1
    await expect(boardItems.first()).toContainText('v1')

    // 2) V011 核对：SCOPE_A 全部通过
    await page.getByTestId('v011-verify').click()
    const v011Report = page.getByTestId('v011-report')
    await expect(v011Report).toBeVisible()
    await expect(v011Report.locator('h5 span')).toHaveText('全部通过')

    // 3) 越权成员只读获取 -> 403 错误提示，快照不被破坏
    await page.getByTestId('operator-select').selectOption('u005')
    await page.getByTestId('consumer-read').click()
    await expect(page.getByTestId('snapshot-error')).toContainText('操作拒绝')
    // 切回有权限成员仍能读到同一份快照
    await page.getByTestId('operator-select').selectOption('u002')
    await page.getByTestId('consumer-read').click()
    await expect(page.getByTestId('snapshot-message')).toContainText('只读')
    await expect(page.getByTestId('card-table')).toBeVisible()

    // 4) 留档核对
    await page.getByTestId('operator-select').selectOption('u001')
    await page.getByTestId('show-archive').click()
    await expect(page.getByTestId('archive-box')).toBeVisible()

    // 5) 消费范围调整（通过 API 模拟范围侧调整）后，旧基线刷新冲突 -> 前端展示 409
    const conflictResp = await request.post('/api/evaluate_board/boards/1/scope/adjust', {
      data: { operator: 'u001', experiments: ['EXPE-0001'] },
    })
    expect(conflictResp.ok()).toBeTruthy()
    const stale = await request.post('/api/evaluate_board/boards/1/snapshot/refresh', {
      data: { operator: 'u001', expected_scope_version: 1 },
    })
    expect(stale.status()).toBe(409)

    // 6) 增量策略：旧范围卡片暂留 + 待移出警告横幅
    await page.getByTestId('refresh-incremental').click()
    const banner = page.getByTestId('inconsistent-banner')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText('EXPE-0002')
    const incrementalCards = page.getByTestId('card-table').innerText()
    expect(incrementalCards).toContain('EXPE-0002') // 旧范围卡片仍展示
    await expect(boardItems.first()).toContainText('待移出')

    // 7) 全量重建后收敛：横幅消失，只剩 EXPE-0001
    await page.getByTestId('refresh-rebuild').click()
    await expect(page.getByTestId('inconsistent-banner')).toHaveCount(0)
    const rebuiltText = await page.getByTestId('card-table').innerText()
    expect(rebuiltText).toContain('EXPE-0001')
    expect(rebuiltText).not.toContain('EXPE-0002')
    await expect(boardItems.first()).toContainText('范围一致')

    // 8) 第二个看板绑 SCOPE_B：V099 未登记口径卡片被拒绝
    await boardItems.nth(1).click()
    await page.getByTestId('scope-select').selectOption('SCOPE_B')
    await page.getByTestId('bind-scope').click()
    await page.getByTestId('refresh-rebuild').click()
    const checks = page.getByTestId('v011-checks')
    await expect(checks).toContainText('V099')
    await expect(checks).toContainText('拒绝：未登记口径')
    const boardTwoCards = await page.getByTestId('card-table').innerText()
    expect(boardTwoCards).not.toContain('V099')

    expect((page as any).__errors).toEqual([])
  })

  test('原有分析看板能力保持：KPI 卡片与运行分析', async ({ page }) => {
    await page.goto('/evaluate_board')
    await expect(page.getByTestId('kpi-card').first()).toBeVisible()
    expect(await page.getByTestId('kpi-card').count()).toBeGreaterThanOrEqual(2)
    await page.getByTestId('run-analysis').click()
    await expect(page.locator('.analysis-bar').first()).toBeVisible({ timeout: 5000 })
    await expect(page.locator('.data-table tbody tr').first()).toBeVisible({ timeout: 5000 })
  })
})
