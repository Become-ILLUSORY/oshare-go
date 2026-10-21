# 回上一轮：0x9985 的写法验证通过，下一步是「我们发通知」

*(2026-10-21 下午，笔记本侧实测结果)*

## 结论

你给的代码依据是对的，我按它实测过了。**反连 → 读 0x9996 → 写 0x9985 这条路
在手机端是被接受的**，不是我上一轮看到的「第一条就 return」。

手机日志（写入被接受，`send response` 就是 ATT 层回了成功）：

```
OShare_BleServer: onConnectionStateChange: newState=2 status=0 **:D5:77:F0:2F:02
OShare_BleServer: putBleDevice BleDeviceInfo(..., transferType=0, hasRead=false)
OShare_BleServer: onMtuChanged mtu=517
OShare_BleServer: onCharacteristicReadRequest()
OShare_BleServer: CHARACTERISTIC_UUID_WIFI          <- 我们读 0x9996
OShare_BleServer: Device state :IDLE
OShare_BleServer: Queried the status info is: 0
OShare_BleServer: onCharacteristicWriteRequest responseNeeded: true offsetValue: 0
OShare_BleServer: onCharacteristicWriteRequest BLE_ACK_DIALOG flag: 1   <- 我们写 01
OShare_BleServer: send response
```

之前超时的真正原因和代码无关：`tools/oshare_ble.py` 反连时**没有先读 0x9996**，
而且它在手机地址下先匹配到了 `service00f7`（那是 `0xbb15` DCP 服务，里面也有一份
`0x9985`），写的是错的那一个。修好之后写入立刻回 `send response`，不再超时。

## 但 ACK 语义确实是我搞错了

你纠正得对：`0x9985` 上的首字节 `1` 走的是 `o()`，也就是 `handlePreviewData`
（缩略图分片），不是「允许」。所以这次写入虽然 ATT 层成功，手机业务层什么都没做——
日志里 `flag: 1` 之后只有 `SuperTouchManager`，没有任何 P2P / ReceiveDialog 动作。

## 现在我看到的手机 0x9996 内容（真实读取）

```
02:00:00:00:00:00&3&M...base64...&1&1&{"info":"...","salt":"...","sign":"..."}
```

字段和 `w.H0()/I0()` 对得上：`<p2p mac>&<3>&<freq 或加密块>&...`，末尾是签名的 JSON。
也就是说**手机此时已经是 GO 候选**，它把 P2P 信息放在自己的 0x9996 里等对端来读，
而不是等对端先 notify 0x9989。

## 于是有三种走法，我需要你判一下哪个才是原厂顺序

1. **我们读手机的 0x9996 就算「拿到凭据」**——那么下一步应该是直接去连它广播的
   P2P 组（`mac&3&...` 里的频段/SSID 还需要解开那段 base64），跳过 BLE ACK。
   这条路不需要任何通知，最省事。
2. **还是要我们 notify 0x9989**（REQUEST_GO）先立 GO，手机再回 notify 0x9985。
   这就回到 BlueZ 的 CCCD 问题，需要走 AcquireNotify / 打补丁 / ESP32。
3. **手机当接收方时**才用 notify 0x9985 那条（`w.i1()` 的 Base64 AP JSON），
   而我们现在的场景是手机当发送方，根本用不到它。

请确认：在「手机发送 → Linux 接收」这个方向上，手机读完对端 0x9996 之后，
是等 `0x9989` 的通知，还是直接自己起 GO 然后通过别的方式告知端口？

另外两个具体问题：

* `0x9996` 里 `&` 之间那段长 base64（`MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...` 开头）
  是 EC 公钥还是加密后的配置？如果是公钥，我们的 `0x9996` 该回什么——
  同一格式的自家公钥，还是 `{"state":0,"mac":...,"key":""}` 这种简短 JSON？
  现在手机读我们 0x9996 拿到的是后者，`Queried the status info is: 0`，看起来它接受了。
* 手机上 `0xbb15`（DCP）服务里的那套 `0x9985`/`0x9996` 和 `0x9999` 里的是同一份逻辑吗？
  我们现在默认写 `0x9999` 那份。

## 我这边已经做的改动（已推 main）

* `tools/probe_peer.py`：反连手机 → 等 service resolved → 读 0x9996 → 写指定特征，
  `--match` 选设备，特性按完整 UUID 精确匹配并优先 `0x9999`。
* `tools/oshare_ble.py`：去掉死代码，`ack_peer()` 保留但默认关闭（`--no-ack` 的反面），
  等语义确认后决定保留还是删掉。
* 手机 GATT 缓存清干净后重新发现，`0x9999` 下 `0x2902` 依然不出现——
  说明 BlueZ 建了 CCC 但属性表没列出来（和你的判断一致），
  所以通知这条路只能靠 AcquireNotify 或打补丁。

---

# 第二轮实测（笔记本侧）：0x9989 / 0x9985 根本没有 notify 属性

用 `tools/watch_notify.py` 连上手机后读到的手机自身 GATT 声明（完整 UUID 精确匹配，
只取 `0x9999` 服务，跳过 `0xbb15`）：

```
[sub] REQUEST_GO 00009989-...  flags=['read', 'write']
[sub] StartNotify failed char00cf: NotSupported
[sub]   descriptor 00009988-0000-1000-8000-00805f9b34fb
[sub] wrote 0100 to desc00d1                      <- 自定义描述符写成功
[sub] StartNotify failed char00cf: NotSupported   <- 仍然不让订阅
[sub] UPDATE     00009997-... flags=['notify']    StartNotify ok
[sub] ACK_DIALOG 00009985-... flags=['read', 'write']   NotSupported
[sub] WPS_RESULT 00009984-... flags=['write', 'notify'] StartNotify ok
```

对照手机自己建的 GATT 表（`w.G0()`）：

| 特征 | 属性字 | 实际 flags | 能否订阅 |
|---|---|---|---|
| `0x9989` | `10` = read\|write | read, write | **不能** |
| `0x9985` | `10` = read\|write | read, write | **不能** |
| `0x9997` | `16` = notify | notify | 能 |
| `0x9984` | `24` = write\|notify | write, notify | 能 |

也就是说：**手机在 `0x9989` / `0x9985` 上调用 `notifyCharacteristicChanged()`，
但这两个特征在 GATT 里根本没有 notify 属性，也没有标准 CCCD（`0x2902`），
只有一个自定义描述符（`0x9989`→`0x9988`）。**

这一点需要你帮我确认两件事：

1. `w.e.onDescriptorWriteRequest`（反编译失败那段）里，写 `0x9988` 之后手机做了什么？
   是不是 `mDeviceInfoMap[addr].setNotifying(true)` 之类，然后才允许 `y0()` 发出去？
   我们这边写 `0100` 到 `0x9988` 是成功的（回了 response），但手机后续有没有真的发通知？
2. 手机对**真实手机对端**做过同样的事（OPPO↔Xiaomi 抓包时有 `clientConnect`），
   那时它 notify `0x9989` 到底有没有成功？如果 Android 的
   `BluetoothGattServer.notifyCharacteristicChanged()` 在没有 CCCD 时会直接失败，
   那这条链在安卓内部应该是走**自定义描述符**当 CCCD 用，具体见 `0x9988` 的处理。

如果确认手机确实会发，那我们这边只需要解决「BlueZ 客户端不肯订阅一个没有 notify 属性的特征」：

* 试过：`StartNotify` → `NotSupported`；先写 `0x9988` 再 `StartNotify` → 仍然 `NotSupported`；
* 下一步准备试：`AcquireNotify`（如果它不检查 notify 属性），
  或者在本机 BlueZ 上打补丁把 `external_chrc` 的 `BT_GATT_CHRC_PROP_NOTIFY` 放宽，
  或者干脆不订阅——用 `btmon`/HCI 监听自己在空口上收到的 ATT Handle Value Notification
  （ATT 层是明文，只有链路层加密，见下）。

> 注意：BLE 链路本身**没有绑定加密**（我们对手机是 Just Works / 未配对），
> 所以 ATT 里的通知内容是明文的，HCI 抓包能直接看到，不需要 LTK。

顺带一个实测数据点：手机 `0x9996` 读出来是

```
02:00:00:00:00:00 & 3 & MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE... & 1 & 1 & {"info":"...","salt":"...","sign":"..."}
```

你说是 EC P-256 公钥，我验证了：`MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE` 正是
DER SPKI 的 `prime256v1` 前缀，长度 91 字节 base64 → 65 字节点坐标，对得上。

---

# 第三轮实测：五字段 0x9996 让手机立刻开始加密握手

按你的建议把 `0x9996` 改成 `<wifi-mac>&3&<EC P-256 公钥 base64>&1&1`（字段 5 的签名块留空），
手机的反应立刻变了。**它不再走宽松分支，而是真的用我们的公钥做 ECDH + AES-CTR 加密**：

```
OShare_Concurrent_BleClient: onCharacteristicRead,CHARACTERISTIC_UUID_WIFI
OShare_Concurrent_BleClient: mBleAckDialog: true
OShare_Concurrent_EncryptOrDecryptUtil: Current task is: 9056425176006472671,
    client public code is: MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE+MkLDmel0ZKpT7ajLNJewyTe34q3V...
OShare_EncryptOrDecryptUtil: generatorClientCode success.
OShare_EncryptOrDecryptUtil: client encrypt data!
OShare_Concurrent_EncryptOrDecryptUtil: clientEncryption, clientPriCode is MIGHAgEAMBMGByqGSM49...
OShare_EncryptOrDecryptUtil: generate ecdhCode success.
OShare_BleClient: onCharacteristicRead mBleAckPreview: true
OShare_BleClient: writeBleAckCharacteristic ... writeValue.length: 300
OShare_BleClient: createBleAckFragments mMaxDataPerFragment.get(): 509
OShare_BleClient: writeFragment result: true
```

`mBleAckPreview` 从 `false` 变成 `true`，请求从 74 字节涨到 300 字节，
里面第 5 个字段就是它**给我们公钥加密的 `{"ssid","psk","ip","port"}` 候选**：

```
288&1&11&<device name>&<our pubkey echoed>&<88B base64 ciphertext>&<model>&*/*&txt&0&<hash>
      ^f1          ^f3           ^f4 = ECIES(pubkey)             ^f7 mime ^f8 size
```

字段位置和 `j.java` 的解析完全对得上（`f[1]`=version，`f[2]`=pubkey 回显，`f[3]`=密文）。

## 这意味着什么

* **读写 0x9996 就够了**，确实不需要任何通知（你的推断成立）。
* 手机把它的 `{"ssid","psk","ip","port",...}` 用 ECDH(它的临时私钥, 我们的公钥) →
  派生 AES-CTR 密钥加密，然后**写在我们 0x9985 上**。我们只要解这一条写入。
* 也就是说 BlueZ 服务端/客户端两侧的 CCCD 问题**全部绕过**。

## 解密链（从代码抄下来的，待实现验证）

```
pub  = 我方 EC P-256 公钥 (DER SPKI, base64)        # 我们发给手机的
priv = 对应私钥                                      # 只在我们手上
ct   = 字段 4 的 base64                              # 手机写来的密文
```

`f2/a.java` + `g2/a.java` + `d2/*` + `i2/*` 组合出的构造顺序是：

1. 手机侧 `b0.o().l(taskId)` 生成**它自己的** EC 密钥对（`generatorClientCode`），
   私钥日志里是 `MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg...`（PKCS#8）。
2. `g2.a` 里 `h(...)` 放它自己的公钥、`g(...)` 放 `"share"`，
   `f2.a.c(我们的公钥字节)` 做 ECDH → 用 `SHA-256(ECDH 结果 || "share")` 派生 `okm`。
3. `d2.b.a(明文, SHA-256(okm))` = **HMAC-SHA256(明文, key = SHA-256(okm))** —— 不是 AES。
   真正的 AES-CTR 在 `_P1`/`cloudsdk` 那层（`com.allawn.cryptography`），
   `f2.a` 只是负责把 `{sign, salt, info}` 三块拼出来。
4. 私有 SDK 的方法签名我不确定，**这一点需要你帮我看一眼**：
   `com.allawn.cryptography` 里 `AES_CTR_NOPADDING` 的 key/iv 分别是 okm 的哪一段？
   `g2.a` 的 `f14027d`（默认 `new byte[32]`）和 `f14026c`（`"share"`）怎么参与？

拿到这一条就能解出 `{"ssid","psk","ip","port"}`，接着就是：

```sh
wpa_cli p2p_connect <mac> pbc   # 或者 NM / wpa_supplicant 的 p2p_join
# 然后
curl -k "https://<ip>:<port>/download?taskId=..."
```

## 一个旁证

手机在这一轮**真的建了 GO**：`iw dev p2p0 info` → `ssid DIRECT-aa-fastCon`, `type P2P-GO`,
`channel 6 (2437 MHz)`，`ip addr` → `192.168.49.1/24`（和抓包里 `.49.1` 一致）。
组名里的 `aa` 与它 0x9996 字段 0 的 `02:00:00:00:00:00` 不是一回事，
说明这是**持久组**（`persistent_reconnect=1`），下次可以直接 join。

## 下一步我这边做

1. 把 EC 私钥留在磁盘（`/tmp/ec.key`），尝试用 openssl 直接做 ECDH 复现 okm，
   看能不能解开字段 4。
2. 如果解得开，就把「读 0x9996 → 收 0x9985 写入 → 解密 → 拉起 P2P」写成 `tools/oshare_ble.py`
   的完整流程，然后接 `transfer/`。
3. 需要你确认的只有一点：**`com.allawn.cryptography` 的 AES-CTR 密钥派生细节**
   （`okm` 怎么切成 key/iv，`version` 字段干什么用）。

---

# 第三轮的加密链拆完了，有两处对不上，需要你确认

## 1. 手机写来的 300 字节里**没有** AP 凭据

字段 5 是 88 字符 base64 → **严格解码只有 64 字节**，头两字节是 `02 ff`：

```
02 ff 46 64 8c 5e 83 b8 e8 1d 06 96 7d 75 16 11 72 87 7e be bc a2 24 1d
29 50 55 ad eb a5 1a 6f 2e c0 c4 ba c8 da ba 67 25 0f 72 20 43 11 17 75
9b 68 da 63 1d 47 a5 f9 ba 79 3a 29 bb 12 46 e5
```

64 字节 = 2 + 30 + 32，不是 `{"ssid","psk","ip","port"}` 的密文（那至少 100+ 字节）。
对照 `g2.b` 的字段布局，它更像 `{flag/version(2), info(30?), sign(32)}`。

## 2. `okm` 来自 TEE，Linux 侧复现不了

`f2.a.c(pubkey)` 的第一句是：

```java
JSONObject o = new JSONObject(a(this.f13584b, this.f13583a));   // a() -> h2.a.d(...)
```

`h2.a.d()` → `h2.e.f()` 拼出

```json
{"only_key_label":"0","key_label":"SMYijOgbT1JfVMug","info":"<b64 share>",
 "salt":"<b64 2 bytes>","okm_len":"32","hash":"<hmac alg>"}
```

然后交给 `h2.a.e()` → `d.c().e(cmd)` → 失败时回退到
`OplusLockPatternUtils.cryptoEngCommand()` / `CryptoengNative.processCmdV2()`,
也就是**手机的 TEE/安全芯片**。`okm` 是按 `key_label` 从设备密钥派生的，
不是从 ECDH 共享密钥派生的（ECDH 结果 `bArr` 只在下一行被 `SHA-256` 后当 **HMAC 的消息**
用：`d2.b.a(sha256(pubkey), okm)`）。

我在 Linux 上做了 ECDH（`openssl pkeyutl -derive`，shared = `202ec77c…67639f`），
拿它当 HKDF 的 salt/IKM 穷举了 2 字节随机数（65536 次）+ 四种 info/label 组合，
**没有任何一组能对上那 32 字节签名**，符合「okm 在 TEE 里」的判断。

## 所以需要你回答三个问题

1. **AP 凭据到底从哪来？** 手机当发送方时，它把自己的 `{"ssid","psk","ip","port"}`
   写进哪一条特征？是 `0x9985` 的**通知**（`i1(softap.d.d(...))`，Base64 明文 JSON），
   还是写完之后要我们**反读**它某个特征（比如 `0x9995`/`0x9986`）？
   现在这 300 字节显然不是凭据。
2. `h2.a.e()` 的 TEE 路径在**非 OPPO 设备**上有没有软件回退？
   `d.c().e()` 是本地实现，如果它只是在没有 TEE 时失败、而 `f14460a` 有本地版本，
   那 okm 也许可以在 Linux 上算；需要你把 `d2.d`/`d.c`（`h2.d` 包）看一眼。
3. `0x9989` 的 **notify**：既然手机把 `0x9989` 声明成 read|write（没有 notify 属性），
   它调 `notifyCharacteristicChanged()` 在 Android 上到底会不会真的发出去？
   如果会，那我们的问题就只剩「BlueZ 客户端不给订阅」——但 `0x9989` 我们连 StartNotify 都被拒
   （`NotSupported`，因为属性位不够）。这条需要手机侧确认它到底有没有在空口上发过。

## 已经从手机侧拿到的确定事实（可以直接用）

* 手机建了持久 P2P 组：`iw dev p2p0 info` → `ssid=DIRECT-aa-fastCon`, `type=P2P-GO`,
  `channel 6 (2437 MHz)`；`ip addr` → **`192.168.49.1/24`**。
  `p2p_supplicant.conf` 里 `persistent_reconnect=1`，所以这个组会复用。
* 手机侧加密公钥每次握手都换（两次抓到的分别是 `…VYSnArbqtw+l0p7GE+WVlhVp…`
  和 `…9QGccRzEnC13FzypHxg4lNfM…`），是临时密钥，不能提前算。
* 我们的 `0x9996` 现在回 `<mac>&3&<pub>&1&1`，手机**确实会解析并回显我们的公钥**
  （字段 4 = 124 字节 base64，和我们发的一模一样），并且 `mBleAckPreview` 置 true。

## 我这边下一步（不等回复也能做）

1. 试着用 `wpa_cli`/`wpa_supplicant` 直接 **join 那个持久组**（`DIRECT-aa-fastCon`），
   看能不能不经凭据就连上（持久组第二次加入往往不需要重输 PSK）。
   如果连上，就直接探 `192.168.49.1` 的 TLS 端口，绕开整个 BLE 凭据交换。
2. 把 `tools/oshare_ble.py` 的 ACK 步骤改成「只记录、不反连」，避免干扰手机状态。

---

# 手机端回答（第四轮）：凭据走 0x9986 的写入，TEE 没有软件回退

## Q1 手机当发送方时，AP 凭据写在哪

**写在 `0x9986`（START_WITH_WPS_INFO），而且是一条普通的 GATT 写入，不是通知。**

```java
// ble/impl/j.java:1596  d(k)
if (connected && discovered) {
    log("writeStartCharacteristic CHARACTERISTIC_UUID_START_WPS_INFO");
    f2(j1(kVar), k9.c.f19002c);        // f19002c = 0x9986
}
```

`j1(kVar)` 拼的就是凭据串（`ble/impl/j.java:1842`）：

```java
sb.append(mac.replace(":", ""));   // 对端 wifi mac，纯 hex，12 字符
sb.append(3);                      // 协议版本，紧贴 mac，没有分隔符
sb.append("&").append(freqClass);
sb.append("&").append(11);         // 命令字 11
sb.append("&").append(ssid);
sb.append("&").append(psk);
...                                // 未配对时这些字段是明文（配对后才走 b0.o().e() 加密）
```

对端（`w.java` 的写处理）收到后，先按首字段长度累积分片，拼够后去掉前缀再解析 ——
所以你会看到首字段是总长度（例如 `288&`）。

**你看到的 300 字节不是凭据**：`mBleAckPreview: true` 说明手机走的是**图片预览**分支
（`this.M = e0.j(flag)`，由分享内容的类型决定，分享图片时置 true），
那条 300 字节是“分享请求 + 预览图”，走 0x9985。

> 建议直接验证：手机上分享一个**非图片**文件（PDF/zip），`mBleAckPreview` 会是 false，
> 手机就应该直接落到 `writeStartCharacteristic CHARACTERISTIC_UUID_START_WPS_INFO`，
> 你会在我们 0x9986 上收到形如 `NNN&<mac>3&<freq>&11&<ssid>&<psk>&…` 的写入。
> 这条是**写**，BlueZ 完全支持，不需要任何通知。

## Q2 `h2.a.e()` 有没有软件回退 —— 没有

```java
// h2/d.java:60
public byte[] e(byte[] cmd) {
    try {
        return (OplusBuild.getOplusOSVERSION() < 26 || !d())
                ? f(cmd)                                     // 也是 HIDL
                : CryptoEngManager.getInstance().cryptoEngCommand(cmd);
    } catch (NoClassDefFoundError e) {
        return f(cmd);
    }
}
// h2/d.java:69  f()  ->  a(list) -> HIDL processCmdV2
```

两条路**都**落到厂商的加密引擎（HIDL / TEE），没有纯 Java 实现。
`h2.a.e()` 的输入 JSON 里带着 `key_label`（你们抓到的 `SMYijOgbT1JfVMug`），
`okm` 是用**设备绑定的密钥**按这个 label 派生的，跟 ECDH 共享密钥无关——
所以那段 ECIES 密文在 PC 上**解不开**，这条路可以放弃了。

## Q3 0x9989 的 notify 到底发没发出去

代码层面判断不了：`y0()` 把 `notifyCharacteristicChanged()` 的**返回值丢弃**了。
但从 Q1 的结论看，这个问题的优先级可以降下来：**发送方给的凭据走的是写，不是通知**。
通知只在“手机当接收方/AP 拥有者”的 softap 流程里用（`w.i1()` → Base64 JSON）。

想彻底确认也很容易：真机互传一次，在手机 logcat 里 grep 这两行

```sh
adb logcat -s BleClient:V BleServer:V | grep -E "writeStartCharacteristic|writeBleAckCharacteristic"
```

* 出现 `writeStartCharacteristic CHARACTERISTIC_UUID_START_WPS_INFO` → 凭据是写过去的；
* 只有 `writeBleAckCharacteristic` → 那一轮走的是预览/应答分支。

## 关于持久组

`DIRECT-aa-fastCon` 是持久组（`persistent_reconnect=1`），但那只是**手机自己**记得；
首次加入仍然需要 PSK，而 PSK 就在 Q1 的 0x9986 写入里。所以：

1. 让手机分享**非图片**文件；
2. 收 0x9986 的写入 → 解析出 ssid/psk；
3. `wpa_cli p2p_join` 或直接用 `wpa_supplicant` 的 `p2p_connect ... join`；
4. `curl -k https://192.168.49.1:<port>/download?taskId=…`。

如果第 2 步一直收不到写入，把手机的 `BleServer onCharacteristicWriteRequest` 日志全打出来，
对照 `uuid == 0x9986` 那条，就能看出它是没走到、还是写到了别的特征。

---

# 笔记本侧第五轮：凭据已经拿到了，就在手机的 p2p_supplicant.conf 里

你说 0x9986 才是凭据通道，我用 PDF 复测了两轮，结论是**还差一步**：

* 分享 PDF（`*/*` + `application/pdf`）后手机确实进了 `mBleAckPreview: false` 分支，
  但它在 `0x9985` 上写的仍然是 **288~300 字节的分享请求**，不是 0x9986 的写入；
* 手机侧的 `writeStartCharacteristic CHARACTERISTIC_UUID_START_WPS_INFO` 一次都没出现。

从 `SendTask.L1()/K1()` 看，`d(kVar)`（→ 0x9986 写入）只在
`this.f10334b.m() >= 2` 时走；`m()` 读的是 `v9.d.V()`，也就是**对端广播里的协议版本**。
我们的广告里 `0x6666[0] = 17`，手机解析出来是 17；它自己那套里 >= 2 应该满足，
所以更可能卡在 `mBleAckDialog`（`isSupportBleAckDialog`，`e0.j(flag)` = flag&1）
这条分支上——手机认为我们"支持 ACK 对话框"，于是先发分享请求等我们回 ACK，
而不是直接写 WPS 信息。

## 但这条已经不重要了：持久组的 PSK 就在手机里

```
$ adb shell su -c 'grep -A8 DIRECT-aa /data/vendor/wifi/wpa/p2p_supplicant.conf'
network={
    ssid="DIRECT-aa-fastCon"
    bssid=fe:ad:9e:4d:45:04
    psk="0.364336"
    proto=RSN
    key_mgmt=WPA-PSK
    pairwise=CCMP
    auth_alg=OPEN
    mode=3
    disabled=2
}
```

也就是说：**手机把 GO 的 SSID/PSK 持久化了**，而且 `p2p0` 现在就活着：

```
$ adb shell su -c 'iw dev p2p0 info; ip -4 addr show p2p0'
    addr fe:ad:9e:4d:45:04
    ssid DIRECT-aa-fastCon
    type P2P-GO
    channel 1 (2412 MHz)
    inet 192.168.49.1/24
```

笔记本的无线网卡是 `wlan0`（`4c:d5:77:f0:2f:01`），也支持 P2P（`iw dev` 里有
`P2P-device` 接口）。所以**不需要 BLE 凭据交换**，直接作为 P2P client 加入这个组就行：

```sh
sudo wpa_cli -i wlan0 p2p_group_add          # 或 p2p_find + p2p_connect
sudo wpa_cli -i p2p-wlan0-0 p2p_connect fe:ad:9e:4d:45:04 pbc
# 或者当作普通 Wi-Fi 连（GO 就是 AP）：
sudo nmcli device wifi connect "DIRECT-aa-fastCon" password "0.364336"
```

连上之后 `ip addr` 应该拿到 `192.168.49.x`，然后直接打手机的
`https://192.168.49.1:<port>/download?taskId=…`（端口来自抓包，50856 是那一轮的，
这一轮会变，需要探 `tcpi` 或等 0x9986/0x9953 给）。

## 我从你的抓包里确认的东西

重组的 `cap_p2p.pcap`（sha256 = `a8bc73c6…732192b`，和你 release 里写的一致）：

```
SYN 47859 -> 50856     ← 接收方（Xiaomi .171）先发起
SYN 50856 -> 47859     ← 双向都出现过 SYN，说明是两个方向各开一条
SYN 38695 -> 50856
SYN 50856 -> 38695
```

TLS 1.3，`ServerHello` 里带 `DOWNGRD\x01`（这解释了 `-k` 之外为什么还要接受
自签 `CN=localhost`）；Application Data 是 `17 03 03`，48 KB 量级。
大流量那条是 `192.168.49.1:50856 -> 192.168.49.171:38695`，63.8 MB ——
**GO 是服务器、接收方拨号**，和 HANDOFF 第 5 节一致。

## 下一步（我这边，今晚可以做）

1. `nmcli` 直接连持久组，验证能不能拿到 `.49.x` 地址；
2. 连上后端口扫描 `192.168.49.1` 的高位端口（抓包里是 50856，可以在
   40000–65535 里扫），找到这一轮的 TLS 端口；
3. 用 `transfer/` 里的 TLS 客户端试 `/versionNegotiation`，确认自签证书接受后
   能不能拿到 `taskId`。

如果 1 走不通（网卡被 NetworkManager 占着），我会用 `wpa_cli` 的 p2p 接口试
`p2p_group_add` + `p2p_connect`，需要 root 的话再找你。

---

# 笔记本侧第六轮：**nmcli 直连持久组成功了**（但被 TUN 代理挡住）

按上一节的思路直接连，一次成功：

```sh
$ nmcli device wifi connect "DIRECT-aa-fastCon" password "0.364336"
设备 "wlan0" 成功以 "dead54fd-62f0-4a08-9aa2-8a3aa73c6809" 激活。

$ ip -4 addr show wlan0
    inet 192.168.49.184/24 brd 192.168.49.255 scope global dynamic wlan0
```

**笔记本拿到了 `192.168.49.184`，手机是 `192.168.49.1`，二层是通的。**
所以整条 BLE 凭据通道（0x9986 / 0x9985 / ECIES / TEE）**可以完全跳过**。

## 但 ping 不通，原因是你电脑上的 TUN 代理

```
$ ip route get 192.168.49.1
192.168.49.1 via 198.18.0.2 dev FlClash table 2022 src 198.18.0.1 uid 1000
```

FlClash（mihomo TUN 模式）用 `ip rule 9002 → table 2022` 把**所有非本地流量**
都送进了 198.18.0.0/30，包括 `192.168.49.0/24` 这个局域网段。所以：

* 之前你提醒我「我电脑开了 TUN 模式的 mihomo，不会是这个原因吧」——**确实是这个原因**，
  不过它挡住的是**连上 P2P 组之后的 TCP**，不是 BLE（BLE 走的是蓝牙栈，和 IP 无关）。
* 解决办法二选一：
  1. 在 FlClash 里给 `192.168.49.0/24`（以及 `192.168.49.1`）加 **DIRECT 规则**；
  2. 或临时 `sudo ip rule add to 192.168.49.0/24 lookup main priority 8000`
     （priority 必须小于 9002，才能排在 FlClash 之前）。

## 现在的位置

```
✅ BLE 发现（0x8881 + 0x6666/0x6667 分包）
✅ 手机点选 → GATT 连接 → 读我们 0x9996 → 写分享请求
✅ 持久组 SSID/PSK 已知，笔记本已作为 client 加入，拿到 192.168.49.184
⬜ 打通 192.168.49.1 的 TCP（被 TUN 挡着，加一条直连规则即可）
⬜ 找到这一轮的 TLS 端口（抓包里是 50856，每轮会变）
⬜ /versionNegotiation + /download?taskId=
```

另外：我连上持久组之后，手机侧的 `p2p0` 被拆了（`adb` 也跟着断了），
说明**这个组是互传 App 在分享页里维持的**，关掉 App 或超时就会撤掉。
测试时要保持手机停在互传的发送页。

---

# 笔记本侧第七轮：TUN 关掉后路由恢复，但 P2P 组已经撤了

```
$ nmcli con down FlClash          # 临时停掉 TUN
$ ip route get 192.168.49.1
192.168.49.1 via 172.27.146.14 dev wlan0 src 172.27.146.184   # 正常了

$ nmcli device wifi connect "DIRECT-aa-fastCon" password "0.364336"
错误：未发现 SSID 为 "DIRECT-aa-fastCon" 的网络。
```

路由回到 main 表之后，`wlan0` 在普通 Wi-Fi 上，**手机那轮已经拆了 P2P 组**
（上节里我们一 join，手机的 `p2p0` 就没了，adb 也断了）。
结合 `w.G0()` 与 `SendTask` 的行为，这个组是**互传 App 在发送页里临时维持的**：
手机停在「正在搜索附近设备…」或「等待发送」时才有，退回桌面就撤。

所以正确顺序是：

1. 手机停在互传的发送页，设备列表里能看到 LinuxBox；
2. 笔记本 `nmcli device wifi connect "DIRECT-aa-fastCon" password "0.364336"`
   （或 `wpa_cli p2p_connect`）；
3. `ip route get 192.168.49.1` 必须走 `wlan0`，不是 `FlClash`；
4. `nc -zv 192.168.49.1 40000-65535` 找这一轮的 TLS 端口；
5. `curl -k --resolve localhost:PORT:192.168.49.1 https://localhost:PORT/versionNegotiation`。

## 需要你配合的两件事

1. **FlClash 的 DIRECT 规则**：在 GUI 里给 `192.168.49.0/24` 加一条直连，
   否则每次都得手动 `nmcli con down FlClash`，恢复还得记着 `nmcli con up`。
   （我这边 `nmcli con up FlClash` 已经报「未知的连接」，说明 TUN 配置在 App 里，
   不在 NetworkManager 里。）
2. **adb 断了**：手机无线调试需要重新配对/连接，或者插 USB。
   后续要用 `adb` 读 `p2p_supplicant.conf`（每轮的 PSK 可能换）和 `iw dev p2p0`。
