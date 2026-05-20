import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const TEXT_INPUT_SOURCE = readFileSync(join(ROOT, 'components', 'textInput.tsx'), 'utf8')
const USE_SUBMISSION_SOURCE = readFileSync(join(ROOT, 'app', 'useSubmission.ts'), 'utf8')

describe('submit state consistency invariants', () => {
  it('clears TextInput local source of truth before invoking submit', () => {
    expect(TEXT_INPUT_SOURCE).toMatch(/const clearLocalAfterSubmit = \(\) => \{[\s\S]*?self\.current = false[\s\S]*?vRef\.current = ''[\s\S]*?curRef\.current = 0[\s\S]*?setCur\(0\)[\s\S]*?\}/)

    const plainEnterPath = /const submitted = vRef\.current[\s\S]*?flushParentChange\(\)[\s\S]*?clearLocalAfterSubmit\(\)[\s\S]*?cbSubmit\.current\?\.\(submitted\)/
    expect(TEXT_INPUT_SOURCE).toMatch(plainEnterPath)
  })

  it('marks the UI submitting before async file-drop detection can delay prompt.submit', () => {
    const submittingPatch = USE_SUBMISSION_SOURCE.indexOf("patchUiState({ busy: true, status: 'submitting…' })")
    const detectDrop = USE_SUBMISSION_SOURCE.indexOf("gw.request<InputDetectDropResponse>('input.detect_drop'")

    expect(submittingPatch).toBeGreaterThan(-1)
    expect(detectDrop).toBeGreaterThan(-1)
    expect(submittingPatch).toBeLessThan(detectDrop)
  })
})
