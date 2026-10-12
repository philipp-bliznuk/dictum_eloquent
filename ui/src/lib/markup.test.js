import { describe, expect, test } from 'bun:test'

import { render } from './markup.js'

describe('render', () => {
  test('plain text passes through', () => {
    expect(render('with reckless abandon')).toBe('with reckless abandon')
  })

  test('null and undefined render empty', () => {
    expect(render(null)).toBe('')
    expect(render(undefined)).toBe('')
  })

  test('bold, star em and underscore em', () => {
    expect(render('with reckless **abandon**')).toBe('with reckless <strong>abandon</strong>')
    expect(render('with reckless *abandon*')).toBe('with reckless <em>abandon</em>')
    expect(render('with reckless _abandon_')).toBe('with reckless <em>abandon</em>')
  })

  test('markers inside words and snake_case stay literal', () => {
    expect(render('snake_case_name')).toBe('snake_case_name')
    expect(render('2*3*4')).toBe('2*3*4')
    expect(render('a * b')).toBe('a * b')
  })

  test('bold wrapping a phrase', () => {
    expect(render('he **put paid to** the rumour')).toBe('he <strong>put paid to</strong> the rumour')
  })

  test('escapes html before substitution', () => {
    expect(render('<img src=x onerror=alert(1)>')).toBe('&lt;img src=x onerror=alert(1)&gt;')
    expect(render('**<script>alert(1)</script>**')).toBe(
      '<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>',
    )
    expect(render('"onmouseover="alert(1)')).toBe('&quot;onmouseover=&quot;alert(1)')
    expect(render('a & b')).toBe('a &amp; b')
  })

  test('replacement patterns in user text are inert', () => {
    expect(render('**$&**')).toBe('<strong>$&amp;</strong>')
    expect(render('**$1**')).toBe('<strong>$1</strong>')
    expect(render("**$'**")).toBe("<strong>$'</strong>")
  })

  test('nested and adjacent markers', () => {
    expect(render('**bold *and em* too**')).toBe('<strong>bold <em>and em</em> too</strong>')
    expect(render('*a* *b*')).toBe('<em>a</em> <em>b</em>')
  })

  test('unterminated markers stay literal', () => {
    expect(render('**bold')).toBe('**bold')
    expect(render('*em')).toBe('*em')
    expect(render('_em')).toBe('_em')
  })
})
