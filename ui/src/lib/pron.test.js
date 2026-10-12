import { describe, expect, test } from 'bun:test'
import { renderPron } from './pron.js'

describe('renderPron', () => {
  test('plain respelling passes through escaped', () => {
    expect(renderPron('ə-ˈban-dən')).toBe('ə-ˈban-dən')
    expect(renderPron('<b>')).toBe('&lt;b&gt;')
    expect(renderPron(null)).toBe('')
    expect(renderPron(undefined)).toBe('')
  })

  test('voiced th becomes an underlined pair', () => {
    expect(renderPron('ˈrau̇nd t\u035fhə ˈbend')).toBe('ˈrau̇nd <span class="voiced">th</span>ə ˈbend')
  })

  test('every occurrence is replaced', () => {
    expect(renderPron('t\u035fhə t\u035fhis')).toBe(
      '<span class="voiced">th</span>ə <span class="voiced">th</span>is',
    )
  })

  test('other combining marks are kept', () => {
    expect(renderPron('ˌ(h)wi-t\u035fhər-sə-ˈwe-vər')).toBe(
      'ˌ(h)wi-<span class="voiced">th</span>ər-sə-ˈwe-vər',
    )
    expect(renderPron('ˈfu̇t')).toBe('ˈfu̇t')
  })
})
