const pptxgen = require('pptxgenjs');

const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'Codex';
pptx.subject = '数据集运营服务管理平台功能架构';
pptx.title = '数据集运营服务管理平台功能架构';
pptx.company = '天地奔牛';
pptx.lang = 'zh-CN';
pptx.theme = {
  headFontFace: 'Microsoft YaHei',
  bodyFontFace: 'Microsoft YaHei',
  lang: 'zh-CN',
};

const slide = pptx.addSlide();
slide.background = { color: 'F8FAFC' };
const C = {
  ink: '0F172A', muted: '64748B', white: 'FFFFFF', header: '172554',
  header2: '1D4ED8', blue: 'DBEAFE', blueLine: '60A5FA', violet: 'EDE9FE', violetLine: '8B5CF6',
  teal: 'CCFBF1', tealLine: '14B8A6', amber: 'FEF3C7', amberLine: 'F59E0B',
  gray: 'E2E8F0', grayLine: '64748B', pink: 'FCE7F3', pinkLine: 'EC4899',
  green: 'F0FDF4', greenLine: '86EFAC', band: 'EFF6FF', bandLine: 'BFDBFE',
};
const font = 'Microsoft YaHei';

function box(x, y, w, h, fill, line, radius = 0.1) {
  slide.addShape(pptx.ShapeType.roundRect, { x, y, w, h, rectRadius: radius, fill: { color: fill }, line: { color: line, width: 1.1 }, shadow: { type: 'outer', color: '172554', opacity: 0.10, blur: 2, angle: 45, distance: 2 } });
}
function text(txt, x, y, w, h, opts = {}) {
  slide.addText(txt, { x, y, w, h, fontFace: font, color: opts.color || C.ink, fontSize: opts.size || 10, bold: opts.bold || false, margin: opts.margin === undefined ? 0 : opts.margin, breakLine: false, fit: 'shrink', valign: opts.valign || 'mid', align: opts.align || 'left', paraSpaceAfterPt: 0, bullet: opts.bullet, italic: opts.italic || false });
}
function card(x, y, w, h, title, details, fill, line, note = '') {
  box(x, y, w, h, C.white, line);
  slide.addShape(pptx.ShapeType.rect, { x, y, w, h: 0.34, fill: { color: fill }, line: { color: fill, transparency: 100 } });
  text(title, x + 0.18, y + 0.04, w - 0.36, 0.25, { size: 13, bold: true });
  text(details.join('\n'), x + 0.18, y + 0.48, w - 0.36, h - 0.72, { size: 9.5, color: '334155', valign: 'top', margin: 0.01 });
  if (note) text(note, x + 0.18, y + h - 0.26, w - 0.36, 0.15, { size: 7.5, color: C.muted, italic: true });
}
function arrow(x1, y1, x2, y2) {
  slide.addShape(pptx.ShapeType.line, { x: x1, y: y1, w: x2 - x1, h: y2 - y1, line: { color: C.muted, width: 1, endArrowType: 'triangle' } });
}
function layerLabel(label, y) { text(label, 0.55, y, 12.2, 0.25, { size: 11, bold: true, color: '334155' }); }

slide.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: 13.333, h: 0.78, fill: { color: C.header }, line: { color: C.header } });
slide.addShape(pptx.ShapeType.rect, { x: 8.6, y: 0, w: 4.733, h: 0.78, fill: { color: C.header2, transparency: 15 }, line: { color: C.header2, transparency: 100 } });
text('数据集运营服务管理平台功能架构', 0.55, 0.13, 7.2, 0.25, { size: 22, bold: true, color: C.white });
text('七类核心业务功能 + 平台服务、对外接口及基础设施等扩展支撑能力', 0.55, 0.47, 8.0, 0.16, { size: 9, color: 'DBEAFE' });
text('当前平台功能视图', 10.6, 0.28, 2.1, 0.18, { size: 9, color: 'DBEAFE', align: 'right' });

layerLabel('一、平台服务层', 0.97);
slide.addShape(pptx.ShapeType.roundRect, { x: 0.55, y: 1.18, w: 12.23, h: 0.72, rectRadius: 0.08, fill: { color: C.band }, line: { color: C.bandLine, width: 1 } });
card(0.78, 1.30, 2.05, 0.48, '用户与企业服务', ['注册、实名认证、企业认证', '成员、企业角色和邀请'], C.white, '93C5FD');
card(2.98, 1.30, 2.05, 0.48, '统一权限与多租户', ['企业主租户、平台 RBAC', '数据隔离和操作授权'], C.white, '93C5FD');
card(5.18, 1.30, 2.05, 0.48, '统一 API 网关', ['OAuth2、API Key、路由', '限流、配额和调用统计'], C.white, '93C5FD');
card(7.38, 1.30, 2.05, 0.48, '通知与配置中心', ['短信/邮件、平台角色', '消息通知和系统参数'], C.white, '93C5FD');
card(9.58, 1.30, 2.88, 0.48, '对外接口服务', ['接入连接器适配、资源发布与鉴权', '回调、接口文档和审计'], C.white, '93C5FD');

layerLabel('二、核心业务功能层（七类核心功能及扩展模块）', 2.03);
card(0.55, 2.28, 3.9, 1.42, '数据及服务管理', ['• 数据集、模型、API、SaaS、报告及咨询登记', '• 目录、元数据、版本、价格和交付方式', '• 产品审核、质量审核、安全合规和上架发布', '• API/SaaS 对接规范、OAuth 凭据和网关配置'], C.blue, C.blueLine, '面向数据/服务提供方与平台运营人员');
card(4.72, 2.28, 3.9, 1.42, '订单管理', ['• 购物车、订单创建、取消、支付单和人工确认', '• 订单、支付、交付、售后四域状态机', '• 订阅开通、续费、升级/降级及差价计算', '• 退款、原路退回、订单授权和访问回收'], C.violet, C.violetLine, '面向已实名个人用户与企业租户');
card(8.89, 2.28, 3.89, 1.42, '数据及服务交付管理', ['• 文件、对象存储、API、模型 API 交付', '• SaaS 租户开通、版本切换和权限开通', '• 用户、部门与第三方 SaaS 系统同步', '• 交付任务、验收确认、售后和状态跟踪'], C.teal, C.tealLine, '支撑标准化、可追踪、可审计交付');
card(0.55, 3.93, 3.9, 1.42, '清算管理', ['• 平台服务费、提供方和数据服务方分成', '• 专家费、渠道费、税费等多方费用计算', '• 订单支付、退款和分账追回/抵扣', '• 清算单、台账、调整、锁定和复核'], C.amber, C.amberLine, '确保交易金额可计算、可核对、可追溯');
card(4.72, 3.93, 3.9, 1.42, '审计管理', ['• 登录、认证、权限、产品和订单操作留痕', '• 发布、凭据、交付、退款和清算审计', '• 按租户、用户、资源和时间检索', '• 异常访问、网关错误和告警关联'], C.gray, C.grayLine, '形成业务、技术和安全合规证据链');
card(8.89, 3.93, 3.89, 1.42, '运营平台服务', ['• 平台运营、审核、质量、安全和交付角色协同', '• 产品、订单、交付、清算和审计工作台', '• 网关路由、版本、凭据和调用统计管理', '• 任务进度控制台、配置中心和运营看板'], C.pink, C.pinkLine, '提供统一运营入口和业务协同能力');

layerLabel('三、SLA 保障与基础设施层', 5.58);
card(0.55, 5.83, 3.0, 1.02, 'SLA 保障功能', ['• 健康检查、可用性、错误率和延迟', '• 限流、配额、超时、重试和统一错误', '• 告警、灰度、回退和故障恢复'], C.green, C.greenLine);
card(3.78, 5.83, 3.0, 1.02, '高可用与安全', ['• Kubernetes、APISIX 多副本和负载均衡', '• OAuth2、API Key、密码机和访问控制', '• Redis 共享缓存、网络隔离和安全策略'], C.green, C.greenLine);
card(7.01, 5.83, 3.0, 1.02, '数据与存储支撑', ['• PostgreSQL 业务和审计数据', '• MinIO 文件、数据集和交付对象存储', '• Redis 配额、Token 和分布式状态缓存'], C.green, C.greenLine);
card(10.24, 5.83, 2.54, 1.02, '监控与运营保障', ['• Prometheus 指标、Alertmanager 告警', '• 网关路由、发布、调用和运行日志', '• SLA 报表、审计追踪和运维闭环'], C.green, C.greenLine);

for (const x of [1.8, 4.0, 6.2, 8.4, 11.0]) arrow(x, 1.92, x, 2.2);
arrow(4.45, 2.99, 4.68, 2.99); arrow(8.62, 2.99, 8.85, 2.99);
arrow(4.45, 4.64, 4.68, 4.64); arrow(8.62, 4.64, 8.85, 4.64);
for (const x of [2.0, 6.2, 10.8]) arrow(x, 5.35, x, 5.75);

text('图：当前数据集运营服务管理平台功能架构图（可编辑版）', 4.2, 7.18, 4.9, 0.14, { size: 7.5, color: C.muted, align: 'center' });

pptx.writeFile({ fileName: 'docs/运营平台功能架构图.pptx' });
