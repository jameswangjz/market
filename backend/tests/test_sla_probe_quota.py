"""Execute the real Lua quota plugin; probe exemption never trusts a header."""
from pathlib import Path
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None


@unittest.skipUnless(LuaRuntime, 'requires isolated test dependency lupa')
class ProbeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute('''
redis_calls = 0
package.preload['apisix.core'] = function()
  return {response={exit=function(code,body) return {code=code,body=body} end},
          schema={check=function() return true end}}
end
package.preload['resty.redis'] = function()
  return {new=function() redis_calls=redis_calls+1
    return {set_timeout=function() end,connect=function() return false end} end}
end
ngx={var={uri='/gateway/route/health'},req={get_method=function() return 'GET' end}}
function context(flag,consumer,path)
  return {market_sla_probe_verified=flag,consumer_name=consumer,
    matched_route={value={id='route',labels={market_sla_probe_consumer='probe-consumer',
      market_sla_probe_path=path,market_sla_probe_method='GET'}}}}
end
''')
        self.plugin = self.lua.execute(Path(__file__).resolve().parents[2].joinpath(
            'k8s/market-gateway-quota.lua').read_text())
        self.conf = self.lua.table_from({'route_key': 'route'})

    def test_verified_dedicated_health_probe_never_enters_buyer_redis_counters(self):
        ctx = self.lua.globals().context(True, 'probe-consumer', '/gateway/route/health')
        self.assertIsNone(self.plugin.access(self.conf, ctx))
        self.assertEqual(self.lua.globals().redis_calls, 0)

    def test_missing_server_flag_cannot_bypass_quota(self):
        for flag in (False, None, 'true'):
            with self.subTest(flag=flag):
                ctx = self.lua.globals().context(flag, 'probe-consumer', '/gateway/route/health')
                result = self.plugin.access(self.conf, ctx)
                self.assertEqual(result['code'], 503)
        self.assertEqual(self.lua.globals().redis_calls, 3)

    def test_wrong_consumer_or_path_is_rejected_even_with_verified_flag(self):
        for consumer, path in [('buyer-consumer', '/gateway/route/health'),
                               ('probe-consumer', '/gateway/route/business')]:
            with self.subTest(consumer=consumer, path=path):
                ctx = self.lua.globals().context(True, consumer, path)
                self.assertEqual(self.plugin.access(self.conf, ctx)['code'], 403)
        self.assertEqual(self.lua.globals().redis_calls, 0)


if __name__ == '__main__':
    unittest.main()
