"""Track the approved fluid console layout and display regression work."""
import list_tasks_control as control

control.DOCUMENT_BASIS = '37 控制台多屏自适应布局修复-20261016173000.md'
control.TASKS = (
    ('RSP-FE-001', '控制台全宽布局与原生DPI适配', 'FE', '', '取消普通页及系统设置宽度上限；无重复DPI缩放；保留移动端与弹框约束'),
    ('RSP-QA-001', '笔记本大屏与换屏浏览器验证', 'QA', 'RSP-FE-001', '1366至3440宽度、DPR1及2、窄屏；主区铺满；侧栏折叠；字体稳定'),
    ('RSP-OPS-001', '多屏布局部署与验收', 'OPS', 'RSP-QA-001', '仅更新前端；可信产物；健康检查；待用户在实际双屏验收'),
)

if __name__ == '__main__':
    control.main()
