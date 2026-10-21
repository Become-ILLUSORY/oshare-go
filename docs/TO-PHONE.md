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
