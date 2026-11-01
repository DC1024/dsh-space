import json,os,subprocess,unittest
from pathlib import Path

@unittest.skipUnless(os.name=='nt','Windows PowerShell policy readback')
class GuestFirewallReadbackTests(unittest.TestCase):
    def test_only_exact_equivalent_masks_are_normalized(self):
        env=dict(os.environ,DSH_POLICY_SOURCE=str(Path(__file__).parent/'guest_bridge.ps1'))
        script="""
$ErrorActionPreference='Stop'
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:DSH_POLICY_SOURCE,[ref]$null,[ref]$null)
$function=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Convert-DSHFirewallAddress'},$true)
if(-not $function){throw 'Missing fixed normalization function'}
Invoke-Expression $function.Extent.Text
@('10.0.0.0/255.0.0.0','172.16.0.0/255.240.0.0','192.168.0.0/255.255.0.0','169.254.0.0/255.255.0.0','fc00::/7','fe80::/10','10.0.0.0/255.255.0.0','0.0.0.0/0','Any')|ForEach-Object {Convert-DSHFirewallAddress $_}|ConvertTo-Json -Compress
"""
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','fc00::/7','fe80::/10','10.0.0.0/255.255.0.0','0.0.0.0/0','Any'])

if __name__=='__main__':unittest.main()
