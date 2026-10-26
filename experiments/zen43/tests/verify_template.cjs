/** 离线执行真实模板的结果渲染，核对新旧接口的后续调用参数；仅用 DOM 替身，不联网。 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '../demos/demo_template.html'), 'utf8');
const code = source.slice(source.indexOf('function renderMatchedSections('), source.indexOf('/** 从地址锚点读取场景'));
const texts = [];
const context = {
  TextEncoder,
  /** 接收页面节点 ID（无需使用），返回支持清空的节点替身。 */
  element() { return { replaceChildren() {} }; },
  /** 输入父节点、标签、文本；记录实际渲染文本，返回可供后续添加的节点替身。 */
  appendText(parent, tag, text) { texts.push({ tag, text }); return {}; },
  /** 输入章节树 nodes，返回原文顺序的平铺节点；仅供模板统计。 */
  flattenSections(nodes) { return nodes.flatMap(node => [node, ...context.flattenSections(node.children)]); },
};
vm.createContext(context);
vm.runInContext(code, context);

for (const legacy of [false, true]) {
  const input = legacy ? {} : { article: 'article-url' };
  const output = legacy ? { article: 'article-url' } : {};
  texts.length = 0;
  context.renderResult({ ...output, parent: null, outline: [
    { unit_id: 'chapter', kind: 'section', has_children: false, children: [], title: '测试' },
  ] }, input);
  const parameters = JSON.parse(texts.find(item => item.tag === 'pre').text);
  assert.equal(parameters.structure.article, 'article-url');
  assert.equal(parameters.read.article, 'article-url');
  texts.length = 0;
  context.renderResult({ ...output, match_tree: [
    { unit_id: 'chapter', title: '测试', hits: [], children: [] },
  ] }, input);
  assert.equal(JSON.parse(texts.find(item => item.tag === 'pre').text).article, 'article-url');
}
console.log('模板后续调用参数：新旧接口的结构与命中场景均通过。');
