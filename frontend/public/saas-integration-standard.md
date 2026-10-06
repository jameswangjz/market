# SaaS 应用第三方接口对接规范

平台通过 OAuth2 `client_credentials` 获取令牌，主动调用第三方 SaaS 的 `OPEN`、`CLOSE`、`RENEW`、`CHANGE`、`USER_ASSIGN`、`USER_UNASSIGN`、`DEPT_CREATE`、`DEPT_REMOVE` 接口；成功返回 `ret=0`，失败返回 `ret=1` 和 `msg`。第三方应支持月付、季付、年付、永久授权，关闭租户后至少保留 1 个月，并支持版本升级、降级和用户/部门同步。平台对每次调用记录幂等操作台账。
