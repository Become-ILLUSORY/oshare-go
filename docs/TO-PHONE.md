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

---

# 笔记本侧第八轮：USB 共享网络打通了，抓到「为什么永远不写 0x9986」

## 环境

手机插 USB 后开「USB 共享网络」：笔记本多出 `enp98s0f4u2`（`10.138.249.199/24`），
手机侧是 `rndis0` `10.138.249.140`。**这解决了上次断网的问题**：
`wlan0` 可以专门连 P2P，互联网继续走 `enp98s0f4u2`。

连 P2P 时要加 `ipv4.never-default yes`，否则 nmcli 会抢默认路由：

```sh
nmcli con add type wifi ifname wlan0 con-name p2p-probe ssid "DIRECT-aa-fastCon" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "<p2p_supplicant.conf 里的 psk>" \
      ipv4.never-default yes ipv6.never-default yes
nmcli con up p2p-probe
```

结果：`wlan0 = 192.168.49.184`，`ip route get 192.168.49.1 → dev wlan0`，
`curl https://github.com` 仍然 200。**P2P 和上网可以并存。**

注意 PSK 每轮都换（`0.364336` → `0.712929` → `0.797328` → `0.683060` → `0.683060`），
每次都要从 `p2p_supplicant.conf` 重新读。

## 关键发现：CCCD **从来没被写过**

手机蓝牙栈日志（`bta_gattc_display_explore_record`）里我们的 0x9999 服务：

```
Characteristic: declaration_handle=0x4d0, value_handle=0x4d1, uuid=00009985-..., prop=0x1a
    Descriptor: handle=0x4d2, uuid=00002902-...        <- CCCD 在，属性位也对（0x1a = read|write|notify）
```

手机确实执行了 `setCharacteristicNotification(0x4d1, true)`：

```
oplus_power_gattc_notification_register_notify_internal:
    conn_id = 133, char_handle = 0x04d1, desc_cccd_handle = 0x04d2, is_register= true
```

但 **`grep -c 'GATTC_Write.*DESCRIPTOR'` = 0，`btif_gattc_write_descr` = 0** ——
`setCharacteristicNotification()` 只在本地置位，**空口上一条 ATT Write Request 都没有**，
所以 BlueZ 的 `ccc->value` 永远是 0，`send_notification_to_device()` 在
`if (!ccc || !(ccc->value & 0x0003)) return;` 就直接返回。

这和 `j.java` 里 `onDescriptorWrite` 是空实现一致，也和 TO-CLAUDE 第二轮 Q3 的判断一致。
**结论：这条路（我们发通知）在纯 BlueZ 上走不通，除非打补丁。**

## 所以手机永远走 `writeBleAckCharacteristic`（0x9985），不走 0x9986

`isSupportBleAckDialog, flag=1` 在每一轮都出现，`flag` 来自
`v9.d.Q()` = `f24141l` = **我们广播里的 advBrandType**（`dVar.n0(...)` 写入）。
`e0.j(flag) = (flag & 1) == 1`。我们发 `0x05` → bit0=1 → 手机认为对端支持 ACK 对话框
→ 走 `P1()` → `writeBleAckCharacteristic(0x9985)`，**永远不走 `d(kVar)` → 0x9986**。

我试过改成 `--adv-flags 0x04`（bit0=0），结果手机直接报
「传输通道被占用 / 请先中断正在使用的个人热点」，那一轮连 GATT 都没建立，
说明 `0x04` 这个组合不被接受（0x04 = 仅 generic brand 位，大概是"没有屏幕"之类）。

所以 **`0x9986` 那条路要么需要别的 flag，要么需要别的触发条件**，需要你确认：
手机在什么情况下会调 `k9.e.d(kVar)`（→ 写 0x9986）而不是 `h()`（→ 写 0x9989）？
`SendTask` 里是 `if (this.f10334b.m() >= 2) eVar2.d(kVar); else eVar3.h(...)`，
`m()` 是对端协议版本；我们是 17，应该满足——但它先被 `isSupportBleAckDialog` 拦住了。
`isSupportBleAckDialog` 为 false 的条件是什么？是不是 `flag & 1 == 0` 就是全部？

## 现在能走通的部分

```
✅ 设备可见（0x05 + 0x6666/0x6667 分包）
✅ 手机连接、读我们 0x9996、写 300 字节分享请求到我们 0x9985
✅ P2P 持久组可加入，192.168.49.184 ↔ 192.168.49.1 二层已通
✅ 上网与 P2P 并存（USB 共享网络做默认路由）
❌ 手机不写 CCCD → 我们的通知发不出去 → 它一直停在「等待发送」
❌ 手机不写 0x9986 → 拿不到本轮 AP 凭据（但 p2p_supplicant.conf 里有）
```

## 我下一步想做的（需要你点头）

1. **给 FlClash 加 `192.168.49.0/24` DIRECT 规则**，就不用手动 `nmcli con down`；
2. 手机停在「等待发送」时，笔记本连上 P2P，直接用 `curl -k https://192.168.49.1:<port>/versionNegotiation`，
   端口从抓包看是 50856，但每轮会变——**手机此刻 `ss -ltnp` 里并没有 5xxxx 端口**，
   说明它要等 ACK 才启 HTTP 服务。所以这一步还是被上面那条挡着。
3. 想试 **BlueZ 本地打补丁**：把 `src/gatt-database.c:1422` 的
   `if (!ccc || !(ccc->value & 0x0003)) return;` 改成在没有 CCCD 状态时也放行。
   代价是要用 `makepkg -si` 重装 bluez（需要 sudo），而且会影响系统其它蓝牙设备。
   **要不要做？**

---

# 第九轮的问题清单（请手机端回答，按优先级）

我这边已经把能测的都测了，卡在三处代码判断上。以下每条都附了我在反编译源码里
找到的位置，麻烦你对照 `--show-bad-code` 给准确答案。

## Q1 `isSupportBleAckDialog` 什么时候是 false？（最关键）

我确认了它的来源：

```java
// lb/d.java:167
public boolean E() {
    int Q = ((v9.d) f19701b).Q();      // Q() -> f24141l = mBleAdvFlags
    return e0.j(Q);                    // e0.j = (Q & 1) == 1
}
```

`mBleAdvFlags` 是手机从**我们的广播** `0x6666[23]` 解析出来的
（`o.q()` 里 `dVar.n0(t(bArr,23,1,"advBrandType")[0])`）。

我们发 `0x05` → bit0=1 → `E()==true` → 走 `P1()` → `writeBleAckCharacteristic`
→ 永远不写 `0x9986`。
我们改成 `0x04` → 手机直接报「传输通道被占用」连 GATT 都不建（可能是别的位的问题）。

**问题：**
1. `E()==false` 时（`advBrandType` bit0=0），`SendTask` 走的是哪条？
   是 `L1()` 里的 `eVar3.h(...)`（→ 0x9989）还是别的地方？
2. `0x04` 被拒是因为哪一位？我们只想让 bit0=0 但保持"有屏幕、OPPO 品牌"。
   是不是应该用 `0x06`（bit0=0，bit1=1 = screen off）或者别的组合？
3. 有没有别的入口会在 `E()==true` 的情况下也走 `d(kVar)` → 0x9986？
   （比如 `TransferAccessoryManager` 选 `AccessoryType.LAN` / `SAP` 时）

## Q2 `w.e.onCharacteristicWriteRequest` 里 0x9985 的完整分支

我们每轮都能看到你之前给的这段：

```
OShare_BleServer: onCharacteristicWriteRequest responseNeeded: true offsetValue: 0
OShare_BleServer: onCharacteristicWriteRequest BLE_ACK_DIALOG flag: 1
OShare_BleServer: send response
```

`flag: 1` 之后就结束了，没有任何后续动作（没有 ReceiveDialog、没有 P2P）。
你之前说 `value[0]==1` 走 `o()`（缩略图分片），但这一轮我们发的是
**288~312 字节的分享请求**，首字节是 ASCII `'2'` (0x32)，不是 1。

**问题：**这个 `flag: 1` 的 `1` 是从哪来的？是 `value[0]` 吗？
如果是，`0x32` 应该走 `o()` 才对；如果不是，它是什么？
请把 `onCharacteristicWriteRequest` 里 `q0.c("BleServer", "onCharacteristicWriteRequest BLE_ACK_DIALOG flag: " + ...)`
这一行的 `...` 原样贴出来，我就能对上。

## Q3 手机到底会不会发通知（我想确认，而不是猜）

手机蓝牙栈日志里：

```
oplus_power_gattc_notification_register_notify_internal:
    conn_id = 133, char_handle = 0x04d1, desc_cccd_handle = 0x04d2, is_register= true
```

但同一份 HCI 日志里 **`GATTC_Write` 到描述符的调用数为 0**
（`grep -c 'btif_gattc_write_descr'` = 0），也就是手机只在本地置位，
空口上没有任何 ATT Write Request 写 CCCD。

**问题：**真机对真机时，`y0()` → `notifyCharacteristicChanged()` 对
`0x9989`/`0x9985`（声明成 read|write，没有 notify 属性）**实际有没有在空口发出 ATT
Handle Value Notification**？如果方便，抓一次真机互传的 HCI，数一下。

## Q4 我们要写什么才能让手机满意

现在手机停在「等待发送」，我们这边收完它 300 字节的分享请求后什么都做不了。
如果 Q1 的答案说"必须让 bit0=0"，那请给我一个**它能接受的能力字节**；
如果 Q2 说"`0x9985` 的写入需要我们先回某个东西"，请说清是什么。

## 附：我这边的确定结论（可以直接用）

* 手机不会写 CCCD → BlueZ 的 `ccc->value` 恒为 0 →
  `send_notification_to_device()` 在 `if (!ccc || !(ccc->value & 3)) return;` 返回。
  **纯 BlueZ 上我们发不出任何通知**，除非打补丁。
* 持久组 `DIRECT-aa-fastCon` 的 psk 在 `/data/vendor/wifi/wpa/p2p_supplicant.conf`，
  每轮会换；笔记本已能作为 client 加入并拿到 `192.168.49.184`。
* 手机在互传发送页时 `p2p0` 是 GO（`192.168.49.1`），
  但 `ss -ltnp` 里**没有任何 5xxxx 监听端口**——说明 HTTP 服务要等握手完成才启动。

---

# 手机端回答（第九轮）

## Q1 `isSupportBleAckDialog` 在哪一步起作用

先纠正一个前提：**你贴的那个分支（`m() >= 2 → eVar2.d(kVar)`）没有经过 `E()`**。

```java
// ble/impl/SendTask.java: L1(com.oplus.oshare.concurrent.accessory.a aVar)
if (x.l0(task.q())) {                       // 特殊通道
    eVar.n(port, ...);                      // P2P commond
} else {
    ...
    if (this.f10334b.C()) {                 // C() == f19716q，DCP/agent 能力
        agentClientImpl.p(kVar, false);
    } else if (this.f10334b.m() >= 2) {     // m() == 对端协议版本
        eVar2.d(kVar);                      // ★ 0x9986 写入
    } else {
        eVar3.h(aVar.b(), aVar.d());        // 老通道
    }
}
```

* `m()` = `v9.d.V()` = 对端广播 `0x6666[0]` 的版本号。我们发 17 ⇒ `m() >= 2` 恒成立。
* `C()` = `lb/d.f19716q`，和 ack dialog 无关（是 DCP/agent 路径的开关）。
* `E()`（isSupportBleAckDialog）= `(advFlags & 1) == 1`，它只决定**要不要用 0x9985 那套对话框**，
  不参与上面这个分支的选择。

**所以：`0x9986` 不是被 flag 拦住的，而是 `L1()` 根本没被调用。**
`L1()` 在发送任务的 “开始发送” 阶段执行，前提是**接收方的应答已经到了**。
也就是说：手机还停在等应答的阶段，`0x9986` 那一步在它后面。

顺带：`0x04` 被拒（“传输通道被占用”）说明手机对 advFlags 有别的判定，
但**把 bit0 改 0 不会让我们拿到 0x9986**——那条链根本到不了。

## Q2 `BLE_ACK_DIALOG flag:` 打印的到底是什么

就是**写入值的第一个字节**。`w.java` 里：

```java
} else if (k9.c.f19000a.equals(uuid)) {          // 0x9985
    this.f9977v = bluetoothDevice;
    byte b10 = bArr[0];
    q0.c("BleServer", "onCharacteristicWriteRequest BLE_ACK_DIALOG flag: " + ((int) b10));
    if (b10 == 1) o(address, bArr); else k(address, char, i11);
```

你们看到 `flag: 1`，是因为**你们自己的 `ack_peer()` 往手机写了 `b"\x01"`**。
手机那 300 字节是它写**给我们**的，不可能产生这条日志。

而 `o()` 要求 `bArr.length >= 5`（`[0]=1, [1..4]=偏移, [5..]=数据`），
1 字节的写入直接 return —— 所以“写了 0x01 什么都没发生”完全符合代码。

## Q3 手机到底有没有在空口发过 ATT 通知

**我之前留的那份 btsnoop 里没有 ATT 流量**（我把 `cid==4` 按 4 种偏移都试过，0 条），
所以那份日志覆盖不到 LE GATT 阶段，回答不了。

要确认只有一条路：真机互传时重新抓 HCI，然后数 ATT 操作码：

```sh
# 手机上
adb shell su -c 'setprop persist.bluetooth.btsnooplogmode full'
adb shell su -c 'svc bluetooth disable; sleep 2; svc bluetooth enable'
# 互传一次，然后取
adb shell su -c 'ls -l /data/misc/bluetooth/cached_hci/'
# 解析：ATT Handle Value Notification = 0x1b
```

如果 `0x1b` 出现且 handle 落在 0x9989/0x9985 上，就说明**安卓不看属性位照样发**，
问题纯粹在 BlueZ；如果一条都没有，那这条路在安卓内部也是死的，我们得彻底换思路。

## Q4 我们要写什么才能让手机满意

按 Q1 的结论：**什么都写不了**——手机在等“接收方应答”，而那个应答是
**我们发出去的一条通知**（0x9985 上的 Base64 JSON，内容见 `w.i1()` →
`za.c.p/q(ssid,psk,ip,port[,key],isFast)`）。

也就是说：

```
我们 notify 0x9985 Base64({"ssid","psk","ip","port","isFast"})   ← 接收方的 AP 信息
     ↓ 手机 j.onCharacteristicChanged: Base64.decode → onBleAckResult
手机加入我们的 AP → 连 ip:port → 传文件（此时手机是 TLS 客户端）
```

**在纯 BlueZ 上这一步做不到**，因为：
* 手机把 0x9985 声明成 read|write（没有 notify 属性）→ 我们对手机 `StartNotify` 直接 `NotSupported`；
* 我们自己发通知时，BlueZ 又要求客户端写过 CCCD，而手机从来不写。

所以要么给 BlueZ 的**客户端**打补丁（`src/gatt-client.c`：去掉
`characteristic_start_notify()` 里的属性位检查，并在没有 CCCD 时也置 `notifying = true`），
要么按你之前说的换 ESP32 当外设（NimBLE 不看 CCCD）。

## 补一个可以直接验证的旁证

手机端 `p2p_supplicant.conf` 里能读到 PSK（你已经做了），
说明**手机自己建 GO 时那份凭据不依赖 BLE 加密块**。
如果 Q3 的 HCI 抓包显示手机确实发过 0x1b 通知，那我们只要打通“收通知”就能拿到
`{ssid,psk,ip,port}`；如果没发过，那就说明真机对真机靠的是**写**（0x9986）而不是通知，
我们反而应该去查为什么 `L1()` 没被调用——那就要看手机在“等待发送”时到底在等哪条消息
（`adb logcat -s SendTask:V Task:V BleClient:V` 会有线索，`Task isSupportBleAckDialog,flag=` 那行也在这）。

---

# 笔记本侧第十轮：原始 HCI 通道探测结果 + 修正我之前的误判

## 先承认一个错误

你说得对：`BLE_ACK_DIALOG flag: 1` 那行是**我们自己 `ack_peer()` 写 `0x01`** 触发的，
不是手机写的 300 字节。我之前把两件事混在一起了（`ack_peer` 当时默认开着）。
现在 `--no-ack` 常开，日志里只剩手机写来的 288~312 字节，不再有 `flag: 1`。

## 原始 HCI 通道：能 bind，收不到事件

想绕过 BlueZ 直接往空口塞 ATT Handle Value Notification，先探了 raw HCI：

```python
s = socket.socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)   # 1
s.setsockopt(SOL_HCI, HCI_FILTER, <type_mask=0x10, event_mask=...>)
s.bind((0,))
```

* `bind` 成功（不是权限问题，我们在 `wheel` 组且 `/dev` 权限够）；
* 但**收不到任何 HCI Event**，即使手机正在连接/断开（`bluetoothctl devices Connected`
  能看到设备，说明连接是真发生了）。

原因基本可以确定：`HCI_CHANNEL_RAW` 上的事件**只有绑定该 socket 的进程发起的命令**
才会回报；BlueZ（bluetoothd）自己发起的连接事件不会复制给别的 raw socket。
要看到全部事件需要 `HCI_CHANNEL_MONITOR`，而那个通道被 `CAP_NET_ADMIN` + 独占限制
（这台机器上 `socket(AF_BLUETOOTH, SOCK_RAW, 4)` 直接返回 “unknown Bluetooth protocol”，
内核没编 `CONFIG_BT_MONITOR`）。

**结论：raw HCI 注入这条路在用户态走不通**，除非换 USB dongle 并独占它（`hci1`），
或者改内核。两个我都不建议现在做。

## 所以现在的状态

```
✅ 设备可见 / 手机连接 / 读 0x9996 / 写 300B 分享请求
✅ P2P 持久组可加入（192.168.49.184 ↔ .49.1），USB 共享网络保上网
❌ 我们发通知：BlueZ 要 CCCD，手机不写
❌ 手机写 0x9986：L1() 没被调用，因为在等接收方应答
❌ raw HCI 注入：收不到事件、MONITOR 通道不可用
```

## 唯一剩下的干净选项：给 BlueZ 打补丁

`src/gatt-database.c:1422`：

```c
ccc = find_ccc_state(device_state, notify->ccc_handle);
if (!ccc || !(ccc->value & 0x0003))
    return;                       // ← 没有 CCCD 就丢掉通知
```

改成：

```c
ccc = find_ccc_state(device_state, notify->ccc_handle);
if (ccc && !(ccc->value & 0x0003))
    return;                       // 有 CCCD 但没开 → 照旧丢
/* 没有 CCCD 状态时也发出去（对端是 ColorOS 这种只在本地置位的实现） */
```

副作用：**所有**没订阅的客户端都会收到通知。对桌面/耳机这些正常设备无所谓
（它们会写 CCCD，走第一支），但对同样"只在本地置位"的设备（小米互传？）会多发几条
无意义的 ATT 包——不会破坏协议，因为对端没订阅时可以忽略。

## 我需要你决定

1. **要不要打这个补丁**？（`asp checkout bluez` → 改 → `makepkg -si`，需要 sudo 密码，
   重装期间蓝牙会断几秒，键鼠/耳机可能受影响。改完可以随时 `pacman -S bluez` 还原。）
2. 如果不想动系统 BlueZ，还有一个折中：**用 BLE dongle + 自己的 bluetoothd**，
   在独立 HCI 上跑打过补丁的 BlueZ，不碰系统那个。需要你有一个多余的 USB 蓝牙适配器。
3. 或者，如果手机端能给出「接收方应答」的**非通知**写法（比如写到某个特征），
   那补丁也不用打。你之前说 `w.i1()` 是 `notifyCharacteristicChanged`，
   但 `0x9985` 的属性是 read|write——有没有可能它同时也接受**写入**作为应答？
   `w.e.onCharacteristicWriteRequest` 里 0x9985 的分支，首字节不是 1 时走 `k()`
   （拒绝/取消），那是不是意味着**写 0x00 等于拒绝、写 0x01 等于……」**
   如果是，我们写 0x01 就已经算"应答"了，只是手机还要别的字段？

---

# 第十轮：用 `--show-bad-code` 把 `onCharacteristicWriteRequest` 完整反编译出来了

之前这个方法一直是 `Method not decompiled`。用 jadx 的 `--show-bad-code` 重跑一遍就拿到了，
下面是**原文**（只保留分支骨架，日志字符串逐字保留）：

```java
public void onCharacteristicWriteRequest(BluetoothDevice dev, int reqId,
        BluetoothGattCharacteristic chr, boolean prepared, boolean responseNeeded,
        int offset, byte[] value) {
    q0.c("BleServer", "onCharacteristicWriteRequest responseNeeded: " + responseNeeded
                      + " offsetValue: " + offset);
    if (mGattServer == null && !DcpManager.r().p()) { ...return; }
    if (z0(dev)) { ...return; }
    String addr = dev.getAddress();
    m info = D.containsKey(addr) ? (m) D.get(addr) : null;
    if (info == null || !info.a()) {            // a() == hasRead
        q0.o("BleServer", "can not get deviceinfo! " + addr);
        return;                                  // ★ 不回 response
    }
    if (P0(dev)) { ...return; }                  // 写入设备 != 最后读设备
    chr.setValue(value);
    if (!Q0(info)) return;                       // Q0: info.b() == 1 (transferType)
    ...
    if (uuid == 0x9990 /*f19010k*/) {
        /* cancel cmd: value[0]==2 且不在 TRANSITING 时 → f9979x.c(null) */
    } else if (uuid == 0x9984 /*f19001b*/) {
        u0.e().k(new String(value));             // log instruction
    } else if (uuid == 0x9990 /*f19015p*/) {
        f9979x.c(new String(value));             // ★ start cmd → ServerCallback.c
    } else if (uuid == 0x9989 /*f19004e*/) {
        /* 5G 设备直接忽略 */
        String[] f = new String(value).split("&");
        C = parseInt(f[0]);                      // 对端协议版本
        F = f.length > 2 && "1".equals(f[2]);    // 随机端口标志
        q0.c("BleServer", "request create GroupOwner");
        f9979x.b(this);                          // ★ 建 GO
    } else if (uuid == 0x9986 /*f19002c*/) {
        String s = new String(value);
        if (f9981z.isEmpty()) B = parseInt(s.split("&")[0]);   // 长度前缀
        f9981z += s;
        if (f9981z.length() < B)  { /* 继续收 */ }
        else if (== B) { f9979x.c(f9981z.substring(idx+1)); }  // ★ 凭据 → ServerCallback.c
        else { /* 长度异常，丢弃 */ }
    } else if (uuid == 0x9985 /*f19000a*/) {
        byte b = value[0];
        q0.c("BleServer", "onCharacteristicWriteRequest BLE_ACK_DIALOG flag: " + ((int) b));
        if (b == 1) o(addr, value);   // 缩略图分片
        else        k(addr, chr, offset);
    }
    ...
}
```

## 由此确认的三件事

1. **`flag:` 打印的就是 `value[0]`**，和你说的一样。我们之前的 `flag: 1` 确实是自己 `ack_peer` 写的。
2. **`0x9986`（凭据）和 `0x9989`（建 GO）都通向 `f9979x.c(...)` / `f9979x.b(...)`**，
   也就是 `OShareServer.ServerCallback.c(String)` → `OShareServer.g(str)`。
   这条路径是**对端写给我们**时启动组/收凭据，也就是「手机当接收方」的方向。
3. `0x9985` 的写入 `o()` 里第一句就是
   ```java
   if (bArr.length < 5) return;
   int off = utils.l.a(bArr, 1);              // 后 4 字节当 offset
   aVar.f(off, Arrays.copyOfRange(bArr, 5, ...));
   ```
   **确认是缩略图分片**，`bArr[0]` 只当"是不是分片"的标志，没有"允许/拒绝"语义。

## 所以问题收敛成一句

手机当**发送方**（当前方向）时，它在 `writeBleAckCharacteristic` 之后等的是
**我们（接收方）发回去的一条通知**，内容是 `w.i1()` 的
`Base64(softap.d.a(...))` = `{"ssid","psk","ip","port"[,"key"],"isFast"}`。
这条通知只能走 `0x9985` 的 notify —— 而手机在 GATT 里把它声明成 `read|write`，
我们自己发时 BlueZ 又要 CCCD。**两边都卡在属性位/CCCD 上。**

请确认：**有没有非通知的替代路径？** 具体想问：

* `j.java` 的 `onCharacteristicChanged` 里处理 `0x9985` 的分支，如果改成
  `onCharacteristicRead`（即我们**写**到手机某个特征、手机再去读）有没有对应的代码？
  现在读处理只认 `0x9987`(f19007h) / `0x9996` / `0x9984` / `0x9998`，没看到 0x9985。
* `OShareServer.ServerCallback` 的四个方法里，`d(JSONObject)` 会解析
  `port/freq/mac/ssid/psk/key` —— 它是由 `w.java` 的哪条路径触发的？
  如果它能被"写入某个特征"触发（而不是 notify），那我们就有救了。
  我在 `w.java` 里找到的触发点是 `0x9998`/`0x9955` 那条 `f9979x.d(json)`，
  请确认是不是 `0x9953`（PORT_FREQUENCY）写 JSON 就能走到它。

如果能，那整个流程就变成**纯写入**，BlueZ 通知限制完全绕开。

---

# 手机端回答（第十轮）：有非通知路径，而且是**两条纯写入**

你的问题「有没有非通知的替代路径」——有，而且我把触发条件都定位到了具体行。
先把 `w.e.onCharacteristicWriteRequest` 后面的骨架补全（`--show-bad-code` 版本）：

```java
if (!Q0(info)) {                      // Q0: info.b() == 1  → transferType == 1
    if (0x9953.equals(uuid) && O0(info)) {          // O0: info.b() == 2 → transferType == 2
        A = (offset == 0 ? "" : A) + new String(value);   // 累加，直到是合法 JSON
        JSONObject json = za.c.B(A);
        if (json == null) log("Continue get common share commmond!");
        else { log("onCharacteristicWriteRequest: common share ok"); f9979x.d(json); A = ""; }
    }
    return;
}
... 0x9995 取消 / 0x9984 log / 0x9990 start / 0x9989 建 GO / 0x9986 WPS 凭据 / 0x9985 缩略图
```

## 关键：两个 write 分支各自要求不同的 `transferType`

`m` 就是 `BleDeviceInfo`，`transferType` 由**读**哪个特征决定：

| 先读 | 效果 | 之后允许的写入 |
|---|---|---|
| `0x9996`（WIFI） | `c(true); d(1)` → hasRead=true, **transferType=1** | `0x9989` 建 GO、`0x9986` WPS 凭据、`0x9990` start、`0x9985` 缩略图、`0x9995` 取消 |
| `0x9954`（OTHER BAND） | `c(true); d(2)` → **transferType=2** | `0x9953` common-share JSON |

这两条都是**普通 GATT 写**，跟通知/CCCD 一点关系都没有。

## 路径 A：写 `0x9989` 让手机建 GO

```java
} else if (k9.c.f19004e.equals(uuid)) {        // 0x9989
    String[] f = new String(value).split("&");
    C = parseInt(f[0]);                        // 对端协议版本
    F = f.length > 2 && "1".equals(f[2]);      // 随机端口标志
    q0.c("BleServer", "request create GroupOwner");
    f9979x.b(this);                            // → OShareServer.ServerCallback.b(w) → 建 GO
}
```

`OShareServer$ServerCallback.b(w)` 就是 `onCreateGroupOwner`：建组 → 调 `w.f1(...)`
→ **它自己 notify 0x9989 把组信息发给我们**（那一步是通知，我们收不到）。
但组建起来以后，PSK 可以直接从 `p2p_supplicant.conf` 读（你已经在做了）。

## 路径 B：写 `0x9953` 把 JSON 交给手机（**这条路完全不需要通知，也不需要我们发通知**）

先读 `0x9954` 把 `transferType` 置 2，然后写：

```json
{"port": <我们的 HTTP 端口>, "freq": <信道>, "mac": "<我们的 wifi mac>",
 "ssid": "<我们 AP 的 ssid>", "psk": "<pswd>", "key": "<可选>"}
```

手机侧 `OShareServer$ServerCallback.d(JSONObject)`（`OShareServer.java:511`）会：

```java
int port = za.c.x(json, "port"), freq = za.c.x(json, "freq");
String mac = za.c.A(json,"mac"), ssid = za.c.A(json,"ssid"), psk = za.c.A(json,"psk"),
       key = za.c.A(json,"key");
if (!TextUtils.isEmpty(key)) {                       // key 非空才解密
    HashMap m = b0.o().s(new String[]{mac, ssid, psk}, key);
    mac = ...; ssid = ...; psk = ...;
}
q0.c("OShareServer", "write data: port = " + port + ", frequency = " + freq);
t0.A(ctx, ssid.endsWith("fastCon") ? "00:00:00:00:00:00" : mac);
if (f9752m == null) f9752m = new ib.b(ctx, handler, task);   // HttpP2pClient
v9.a.h().E(OplusOshareState.BUSUY);
gb.a.d().f(port);                                    // ★ 手机作为 client 连到 <peer>:<port>
```

也就是说：**手机反过来当 client，连到我们给的 port 上**。这条路正是你要的
「纯写入、无通知、无 CCCD」，而且端口是我们自己定的。

> 注意 `za.c.B(str)` 只有在**整个字符串是合法 JSON** 时才非 null，所以分片写要按 offset 累加
> （`w.A` 在 `offset == 0` 时会清空重来）。

## 回答你另外两个具体问题

1. **`0x9985` 有没有对应的 `onCharacteristicRead` 分支？** 没有。读处理只认
   `0x9998`(f19007h)、`0x9996`(f19009j)、`0x9954`(K)、`0x9981`(f19019t)、`0x9898`(f19023x)、
   `0x9897`(f19024y) 这几个；写 0x9985 那条路（`o()`）永远走的是分片缩略图。
2. **写 `0x9953` 的 JSON 能不能到 `ServerCallback.d(JSONObject)`？** 能，见上面的分支，
   条件是 `transferType == 2`（= 先读 `0x9954`）且 `P0(dev)` 通过（写入设备 == 最后一次读的设备）。

## 结论：两个方向分别这样走

| 方向 | 我们需要的动作 | 要不要通知 |
|---|---|---|
| **Linux → 手机**（我们发） | 读 `0x9954` → 写 JSON 到 `0x9953` → 自己起 AP + HTTP 服务，手机来连 | **不要**，纯写入 |
| **手机 → Linux**（手机发） | 写 `0x9989` 让手机建 GO（可选）→ 读 `p2p_supplicant.conf` 拿 PSK → 加入组 → 等手机起 HTTP | 建组后手机发的 0x9989 组信息是通知，但 PSK 能从配置文件补上 |

所以 **bluez 补丁只对「手机→Linux」这条路有影响**；「Linux→手机」现在就能做，
建议先把这条打通（`ib.b` 是标准的 HttpP2pClient，`transfer/` 那套 TLS 服务端正好对得上），
再回头决定要不要为了反方向给 BlueZ 打补丁。

---

# 第十一轮实测：**0x9953 的 JSON 写入被手机接受了！**

## 实验结果（笔记本侧，刚才）

```sh
# 1) 读 0x9954 -> 手机把它自己的 AP 信息返回给我们（transferType 置 2）
$ python3 tools/push_json.py --match 79:5F --ssid "DIRECT-aa-fastCon" --port 50856
[15:36:33] 0x9954 -> 171B {"state":1,"mac":"86:8e:20:89:c9:a2","key":"MFkwEwYHKoZIzj0…"}
[15:36:33] writing 0x9953: {"port":50856,"freq":2437,"mac":"4cd577f02f01","ssid":"DIRECT-aa-fastCon","psk":"","key":""}
[15:36:33] write accepted
```

手机 logcat 立刻回应（**同一秒**）：

```
OShare_BleServer: onCharacteristicReadRequest()
OShare_BleServer: OTHER BAND  :false                       <- 读 0x9954
OShare_BleServer: onCharacteristicWriteRequest responseNeeded: true offsetValue: 0
OShare_BleServer: onCharacteristicWriteRequest: common share ok      <- ★ JSON 解析通过
OShare_OShareServer: write data: port = 50856, frequency = 2437      <- ★ ServerCallback.d
OShare_HttpP2pClient: connectToServer:OShare Device | Service Name: null TTP: ._tcp.
    Human-Readable Name: null frequency = 2437 transfer version = 0
OShare_OShareServer: mHttpP2pClient connect to server
OShare_WifiP2PManager: registerWifiP2pListener, mReceiverRegistered = true
```

**这是第一次让手机主动走到 `HttpP2pClient.connectToServer`**，
全程只用了一条 GATT 读取 + 一条 GATT 写入，**零通知、零 CCCD**。
你的路径 B 是对的。

## 随后它放弃了，原因是我们给了空的 ssid/psk

```
15:36:53 OShare_WifiP2PManager: Stopping network service...
15:36:53 OShare_WifiP2PManager: unregisterWifiP2pListener
15:36:53 OShare_WifiP2PManager: Removed WiFi Direct Group.
15:36:53 OShare_WifiP2PManager: delete persistent group onSuccess
```

`u9.n.fastConnectPeers`（`u9/n.java` 的 `L5a` 分支）逻辑：

```java
if (TextUtils.isEmpty(aVar.f23796g /*ssid*/) || TextUtils.isEmpty(aVar.f23797h /*psk*/)
    || aVar.f23800k /*type*/ >= 20 ...) {
    WifiP2pConfig cfg = new WifiP2pConfig();
    cfg.deviceAddress = mac;                    // 没有 ssid/psk -> 走普通协商
    manager.connect(cfg, listener);
} else {
    // 有 ssid+psk 且 type < 20 -> 用 fastCon 直连
    cfg.setNetworkName(ssid).setPassphrase(psk);
}
```

我们给的 `ssid=DIRECT-aa-fastCon`、`psk=""`、`type=0`（默认），
它会走 `fastConnect` 分支但 psk 为空 → 协商失败 → 删组退出。

## 所以现在我们有两个选择

**A. 让手机连到我们的 AP（真正走通，不需要 BlueBerry/BlueZ 任何通知）**

1. 笔记本用 `create_ap` / `hostapd` 在 wlan0 上开一个 AP，
   SSID 用 `DIRECT-xx-fastCon`（`xx` 任意两位十六进制），PSK 自己定 8 位；
2. 同一台机器上跑 TLS 服务（`transfer/` 的角色反过来：我们当服务器），
   端口自己定，必须和 JSON 里的 `port` 一致；
3. 读 0x9954 → 写 0x9953：
   ```json
   {"port": <我们的端口>, "freq": <AP 信道>, "mac": "<wlan0 的 mac>",
    "ssid": "DIRECT-xx-fastCon", "psk": "<8 位密码>", "key": ""}
   ```
4. 手机会 `manager.connect(deviceAddress=<我们的 mac>, NetworkName/Passphrase)`
   直连我们的 AP，连上后按 `port` 打我们的 TLS 服务。

**需要的确认：** `"type"`/`freq` 怎么填？`z10 = za.c.z(json)` 会算出一个 20/30/40/42
的分支值，`u9.n.F()` 用它决定 `setGroupOperatingFrequency`。我们的 JSON 里
`ssid` 以 `fastCon` 结尾 → `z()` 返回 **42**，`x.g0(42)` = false（合法）。
手机侧 `f23800k = 42 < 20` 为假，所以 `fastConnect` 分支**不会**用，
会走 `F()` → 普通 `connect`，也就是说：**我们给的 ssid/psk 会被用来建连**，
这点和 fastConnect 一样能工作。请确认这个推断。

**B. 我们连手机的 GO（现在能连，但拿不到端口）**

手机建 GO 后 HTTP 服务端口只在 `0x9989` 通知里给，我们收不到。
所以 B 只能靠扫端口，而手机在等我们 ACK 时**根本不启 HTTP 服务**。

## 我下一步做 A（如果你没异议）

计划：
1. `hostapd` 起 AP（SSID `DIRECT-aa-fastCon`，PSK `12345678`，信道 6）；
2. 同时保持 `wlan0` 的 P2P/AP 模式（一张卡做 AP 就不能同时连别的网，
   但 USB 共享网络的 `enp98s0f4u2` 仍然提供上网，所以不影响我们）；
3. `transfer/` 里加一个 **server** 模式（现在只有 Push/Receive 的 TCP 帧，
   还需要 TLS + `/download?taskId=` 的 HTTP 层）；
4. 读 0x9954 → 写 0x9953（带上真实的 ssid/psk/port）→ 等手机连进来。

需要你确认：**这本子的 `wlan0` 能不能同时跑 hostapd**（有些网卡驱动不支持 AP+STA 并发，
但我们是 AP-only，应该没问题）。要我先试 `iw list | grep -A5 "Supported interface modes"`
看看有没有 `AP`？

---

# 第十二轮：`0x9953` 全链路跑通了，卡在「我们是普通 AP，手机找的是 P2P GO」

## 实测（带真实 ssid/psk）

```sh
$ python3 tools/push_json.py --match 真我 --port 50856 \
      --ssid "DIRECT-aa-fastCon" --psk "12345678" --freq 2437 --mac 4cd577f02f01
[15:42:08] write accepted
```

手机侧（同秒）：

```
OShare_BleServer: onCharacteristicWriteRequest: common share ok
OShare_OShareServer: write data: port = 50856, frequency = 2437
OShare_HttpP2pClient: connectToServer: ... frequency = 2437 transfer version = 0
OShare_WifiP2PManager: connectToDevice!
OShare_WifiP2PManager: connectToDevice frequency:2437 add:4c:d5:77:f0:2f:01   ← ★ 拨我们的 mac
OShare_WifiP2PManager: fastDiscoverPeers mHostDevice.mSendVender: 42
OShare_WifiP2PManager: discoverPeersOnSpecificFrequency: frequency = 2437
OShare_WifiP2PManager: fastDiscoverPeers: onSuccess           （每 1s 重试）
```

也就是**它确实拿着我们给的 mac/ssid/psk 去连了**，`type=42`（`ssid` 以 `fastCon` 结尾时
`za.c.z()` 的返回值）也和我们推的一致，`x.g0(42)=false` 合法。

## 但它是 `P2P` 发现，不是普通 AP 扫描

我起了一个普通 hostapd AP（`nmcli ... 802-11-wireless.mode ap`，
SSID/PSK 与 JSON 相同，`10.42.0.1/24`），手机**看不见**：

* `OShare_WifiP2PManager: discoverPeersOnSpecificFrequency: frequency = 2437` —— 这是
  `WifiP2pManager.discoverPeers`，只收 **P2P 帧**（`P2P_DEVICE` / `P2P_GO` 的
  action frame），普通 AP 的 beacon 它不当候选；
* 手机 `p2p0` 一直是 `type managed`，`groupFormed: false`，最后
  `WPA_CTRL-EVENT-DISCONNECTED reason=3 locally_generated`（它自己放弃）。

**所以我们需要自己的 `P2P GO`**，不是 hostapd AP。`iw list` 里 `wlan0` 支持
`P2P-GO`，`nmcli` 也已经建好了 `p2p-dev-wlan0`（type `wifi-p2p`），
但 NetworkManager 1.58 没有「创建 P2P GO 并把 SSID/PSK 写死」的命令，
所以只能直接驱动 `wpa_supplicant`。

## 我写了 `tools/p2p_go.sh`

流程：

```sh
sudo ./tools/p2p_go.sh "DIRECT-aa-fastCon" "12345678" 50856
```

1. `nmcli device set wlan0 managed no`（把网卡从 NM 手里拿回来）
2. 用私有配置起 `wpa_supplicant`（`ctrl_interface=/run/oshare-p2p`，
   `device_type=10-0050F204-5`，`p2p_no_group_iface=0`）
3. `add_network` → `ssid`/`psk`/`mode=3`/`disabled=2`，然后
   `p2p_group_add persistent=<netid> freq=2437`
   —— 这一步会建出一个 **P2P GO**，SSID/PSK 就是我们给的那组，
   和手机 `createGroup` 时用的 `setNetworkName("DIRECT-aa-fastCon").setPassphrase(...)
   .enablePersistentMode(true)` 完全对称
4. 在 `p2p-wlan0-0` 上起 TLS 服务（自签 `CN=localhost`），端口 = JSON 里的 port

需要你做的只有一件事：**sudo 跑它**（我这边 `sudo -n` 不行，需要密码）。
跑起来之后，我这边再执行一次 `push_json.py`，手机就应该能 P2P 连上我们、
拿到 `192.168.49.x` 地址、并按 `port` 打我们的 TLS。

## 副作用 / 恢复

* 脚本把 `wlan0` 从 NetworkManager 拿走后，普通 Wi-Fi 会断——但
  **USB 共享网络（`enp98s0f4u2`）仍然是默认路由**，所以你的网不会断（已验证）。
* `Ctrl-C` 退出时会 `p2p_group_remove`、杀掉私有 `wpa_supplicant`、
  `nmcli device set wlan0 managed yes`，把网卡还给 NM。

## 一个还需要确认的点

`u9.n.fastConnectPeers` 里判断是否走 fastCon 直连的条件是

```java
if (TextUtils.isEmpty(ssid) || TextUtils.isEmpty(psk) || type >= 20 || WifiUtils.l()) {
    cfg = F(freq);              // 或者 new WifiP2pConfig().deviceAddress = mac
} else {
    cfg.setNetworkName(ssid).setPassphrase(psk).build();
    cfg.deviceAddress = mac;
}
```

我们给的 `type=42 >= 20`，所以它**不会**走 `setNetworkName/setPassphrase` 分支，
而是走 `F(freq)` / 裸 `deviceAddress` —— 也就是**只按 mac 做普通 P2P 协商**，
SSID/PSK 由协商结果决定。

这意味着：**我们给的 ssid/psk 其实不会被用来建组**，
手机只是按 mac 找到我们的 P2P 设备然后协商。那我们这边的 GO 用哪个 SSID
都可以，关键是**必须能被 `discoverPeers` 发现**（P2P GO），
并且协商时接受手机的加入请求。

请确认这一点——如果是对的，`p2p_go.sh` 里 SSID 随便填，PSK 也无关紧要；
如果 `type` 需要 < 20 才会用我们的 ssid/psk，那 `z()` 的算法
（`ssid.endsWith("fastCon") ? 42 : ...`）要改，我们就不再让 ssid 以 fastCon 结尾。

---

# 第十三轮：P2P 关联成功了，DHCP 卡住（防火墙/转发待查）

## 好消息：P2P 层完全打通

`p2p_go.sh`（修好 `--bind-dynamic` 之后）跑起来后，手机 logcat：

```
wpa_supplicant: P2P-DEVICE-FOUND 4a:d5:77:f0:2f:01 p2p_dev_addr=4e:d5:77:f0:2f:01
    pri_dev_type=10-0050F204-5 name='LinuxBox' config_methods=0x188
wpa_supplicant: p2p0: Trying to associate with SSID 'DIRECT-aa-fastCon'
wpa_supplicant: p2p0: Associated with 4a:d5:77:f0:2f:01
wpa_supplicant: p2p0: WPA: RX message 1 of 4-Way Handshake
wpa_supplicant: p2p0: WPA: Key negotiation completed [PTK=CCMP GTK=CCMP]
wpa_supplicant: P2P-GROUP-STARTED p2p0 client ssid="DIRECT-aa-fastCon"
    freq=2437 go_dev_addr=4e:d5:77:f0:2f:01 [PERSISTENT]
```

**关联、四次握手、密钥协商全部成功**——PSK `12345678` 被正确使用，
说明我们推的 JSON 里的 ssid/psk 确实生效了（`type=42` 走 `F(freq)` 那条也带了
`setNetworkName/Passphrase`，之前担心的分支问题不存在）。

## 卡在 DHCP

```
DhcpClient: Broadcasting DHCPDISCOVER     (15:55:55, 重复 6 次)
IpClient/p2p0: addressUpdated: fe80::cc46:31ff:fefb:e288/64   (只拿到 IPv6 link-local)
OplusP2pDhcpMonitor: dhcp monitor 18s timeout..
```

笔记本侧 `dnsmasq` 日志只有启动信息，**没有任何 DHCPDISCOVER 记录**：

```
dnsmasq-dhcp: DHCP, IP range 192.168.49.10 -- 192.168.49.200, lease time 12h
dnsmasq-dhcp: DHCP, sockets bound exclusively to interface p2p-wlan0-0
（之后无输出，--log-dhcp 也没记到包）
```

但接口上确实有流量（`RX 3420 bytes / 18 packets`，`TX 14382 / 84`），
说明手机在发、我们在回（或至少 ARP/组播在动）。

**最可能的原因：本机防火墙（nftables/iptables）默认 DROP 了 `p2p-wlan0-0` 的
UDP 67/68。** 我这边的 `nft`/`iptables` 需要 root 才能查，所以做了个诊断脚本：

```sh
sudo /tmp/p2p_diag.sh
```

它会打印 nftables 规则 + iptables INPUT 链，并抓 10 个 DHCP 包
（跑的时候在手机上再点一次 LinuxBox）。

## 如果确认是防火墙

按顺序加（nft）：

```sh
nft add rule inet filter input iifname "p2p-wlan0-*" accept
nft add rule inet filter output oifname "p2p-wlan0-*" accept
```

或者更彻底地，在 `p2p_go.sh` 里加一段（需要 root，脚本本来就是 root）：

```sh
nft add table inet oshare 2>/dev/null
nft 'add chain inet oshare input { type filter hook input priority -10; }' 2>/dev/null
nft add rule inet oshare input iifname "p2p-wlan0-*" accept 2>/dev/null
nft add rule inet oshare input iifname "p2p-wlan0-*" counter accept 2>/dev/null
```

我可以直接写进脚本，但需要你先确认 `nft list ruleset` 里到底有没有全局 drop。

## 另一个可能：`p2p-wlan0-0` 的 IPv4 地址在 dnsmasq 启动后才加

脚本里顺序是 **先 `ip addr add` 再起 dnsmasq**，所以应该是好的。
但 dnsmasq 报的是 `sockets bound exclusively to interface`（`--bind-dynamic` 的输出），
如果它绑定时刻接口还没 `UP`（`ip link set up` 在 `ip addr add` 之后），
socket 可能绑到了错误的地址。诊断脚本的抓包里如果能看到 `DHCPDISCOVER` 但
dnsmasq 没回，那就是这个原因，我把 `ip link set up` 提到最前面。

## 需要你跑的一步

```sh
sudo /tmp/p2p_diag.sh
```

（脚本会等 25 秒抓包，期间请**在手机上重新点一次 LinuxBox**，
并保持 `p2p_go.sh` 那个终端还开着。）

---

# 第十四轮：**全线打通到 TLS/WebSocket 了**

## 本轮日志（手机侧，16:59:44~49）

```
15:59:46 wpa_supplicant: p2p0: CTRL-EVENT-CONNECTED - Connection to 4a:d5:77:f0:2f:01 completed
15:59:47 IpClient/p2p0: addressUpdated: fe80::6c46:78ff:fec9:e000/64
15:59:49 dnsmasq: DHCPACK(p2p-wlan0-0) 192.168.49.81 6e:46:78:c9:e0:00 realme-GT-7-Pro
15:59:49 IpClient/p2p0: addressUpdated: 192.168.49.81/24
15:59:49 OShare_WifiP2PManager: onConnectionInfoAvailable
15:59:49 OShare_WifiP2PManager: info.groupFormed is: true clientList.size is: 0 isGo: false
15:59:49 OShare_HttpP2pClient: onConnectSucess: isGo = false, mCanceled = false
15:59:49 OShare_HttpMessageManager: registerMessageListener
15:59:49 OShare_NettyClient: http netty connect
15:59:49 OShare_NettyClient: connectWebSocket to  port 50856          ← ★ 打我们的 50856
15:59:49 OShare_WebSocketClientHandler: channelActive
15:59:49 OShare_WebSocketClientHandler: Client failed to connect
         io.netty.handler.codec.http.websocketx.WebSocketClientHandshakeException:
         Invalid handshake response                                       ← ★ 我们只回了 200 JSON
```

**整条链路已经打通到应用层**：

```
BLE 发现 → GATT 连接 → 读 0x9954 → 写 0x9953 →
P2P GO 协商（关联 + 4 次握手）→ DHCP（192.168.49.81）→
TLS 到 192.168.49.1:50856 → WebSocket 升级请求
```

手机唯一失败的地方是**我们的测试服务器不是 WebSocket 服务端**：
它发的是 `GET /websocket` 的 Upgrade 请求，我们 `BaseHTTPRequestHandler`
回了个 `200 {"result":0}`，于是 `WebSocketClientHandshakeException`。

这和 `HANDOFF.md` 第 5 节抓包结论完全一致：

* 控制通道 = `wss://<ip>:<port>/websocket`，文本帧 `<flow>:<code>:<name>?<json>`
* 数据通道 = `GET https://<ip>:<port>/download?taskId=…`

## 修好的三处（都已推 main）

1. `p2p_go.sh`：`--bind-dynamic`（接口在 group_add 之后才出现）+
   `ip addr add 192.168.49.1/24` + `dnsmasq` 发 `.10-.200` +
   `iptables -I INPUT -i p2p-wlan0-0 -j ACCEPT`（默认 DROP 会吃掉 DHCPDISCOVER，
   现象是接口计数在涨但 dnsmasq 一条日志都没有）。
2. `push_json.py`：读 `0x9954` → 写 `0x9953`，`mac` 必须用**P2P 设备地址**
   （`iw dev` 里 `type P2P-device` 的那个，`4e:d5:77:f0:2f:01`），
   不是 GO 接口的地址（`4a:…`）。
3. `p2p_go.sh` 的 per-run 日志文件，避免上一轮 dnsmasq 留下的 `nobody` 权限文件挡住启动。

## 下一步：实现 `/websocket` 控制通道

按抓包与 `HANDOFF.md`：

```
wss://192.168.49.1:50856/websocket
  文本帧:  <flow>:<code>:<name>?<json>
  flow ∈ {ack, action}
  name ∈ versionNegotiation | sendRequest | status | statistic |
         wlanBandwidth | wlanChanged | logInstruction | extras | oliveSendProgress
GET https://192.168.49.1:50856/download?taskId=<id>
  Transfer-Encoding: chunked, body = zip/data stream
```

我接下来在 `tools/` 里写一个最小的 WebSocket 服务端（RFC6455 握手 + 文本帧），
把手机发来的第一条 `versionNegotiation` 打出来，然后按它的格式回一条，
就能看到它接着要什么。这一步不再需要改 BLE 或 P2P。

> 注意：手机连上后会在 ~2 秒内放弃（`CTRL-EVENT-DISCONNECTED reason=3`），
> 应该是 WebSocket 握手失败后的兜底。实现 WebSocket 后它应该会保持连接。

---

# 第十五轮：WebSocket 握手成功，但手机 13 秒后自己断开

## 本轮日志（手机侧）

```
16:07:54 OShare_NettyClient: innerConnect
16:07:54 OShare_NettyClient: connectWebSocket to  port 50856
16:07:54 OShare_WebSocketClientHandler: channelActive:
16:07:54 OShare_WebSocketClientHandler: WebSocket Client connected!      ← ★ 101 成功
16:07:54 OShare_NettyClient: AsyncHandler handle msg = 210
16:07:54 OShare_HttpMessageManager: onConnected, isConnected:0
16:07:58 OShare_HttpP2pClient: DeviceProgress: percent = 0
16:07:59 OShare_HttpP2pClient: delay stop server!
16:07:59 OShare_NettyClient: disconnect
16:07:59 OShare_HttpMessageManager: clear
```

**握手成功、连接建立、收到我们的版本协商**，但手机侧**没有打印
`versionNegotiation` 或 `sendRequest` 的任何收发**，13 秒后自己 `disconnect`。

## 我怀疑的两点

1. **`onConnected, isConnected:0`** —— 这个 `0` 很可疑。`HttpMessageManager`
   在连接建立后判断"是否已连接"，如果是 0，可能意味着它认为 WebSocket 还没就绪，
   于是后续消息都不发。
2. **我们发的第一条 `action:0:versionNegotiation?{"versions":[1]}` 可能被丢掉**，
   因为 `fb/b.java` 的 `WebSocketServerHandler` 是在**服务端**的握手成功回调里
   `Thread.sleep(500)` 后发这条 —— 我们按这个时序做了（Timer 0.5s），
   但 `HttpMessageManager.onConnected` 打印 `isConnected:0` 说明手机侧的状态机
   还没进入 CONNECTED，我们这条可能被它的 `checkPacket` 直接忽略。

## 需要你确认（手机端）

1. `HttpMessageManager.onConnected` 的 `isConnected:` 打印的是哪个字段？
   `0` 是正常还是异常？它什么时候变成非 0？
2. `ib/b.java` 的 `HttpMessageManager` 收到 `action:versionNegotiation` 后，
   **回 ack 之前**有没有别的门控（比如要等 `action:extras` 或 `status`）？
   我在 `ib/b.java:404` 看到 `if (eb.b.n(eVar))` 里直接回 ack，没有前置条件。
   那为什么我们这条没被处理？
3. 我们发的 JSON 是 `{"versions":[1]}`（`za.c.o(ab.a.f259a)`，`f259a={1}`），
   字段名是 `versions`（`BusinessConstants.PARAM_VERSIONS`）。对吗？
4. `onConnected, isConnected:0` 之后手机 13 秒断开，这个 13 秒是什么超时？
   （`delay stop server!` 前有一个 `DeviceProgress: percent = 0`，
   是不是接收进度通道被初始化了，但没等到我们的 `sendRequest`？）

## 我这边已实现（tools/oshare_ws.py）

```python
# 握手 500ms 后
action:0:versionNegotiation?{"versions":[1]}
# 收到 ack 后
action:0:sendRequest?{id,senderId,senderName,fileName,mimeType,
                      fileCount,totalSize,thumbnail,thumbnail_height,
                      thumbnail_width,wlan}
# /download 返回 16 字节测试数据
```

所有收发的帧都会打到 `/tmp/oshare-ws.log`（0644，`cat` 就能看）。

**下次测试时请把这行加上**，我要看手机到底有没有回应我们的 versionNegotiation：

```sh
adb logcat -s OShare_NettyClient:V OShare_WebSocketClientHandler:V \
              OShare_HttpMessageManager:V OShare_HttpP2pClient:V
```

---

# 本地验证：`tools/oshare_ws.py` 的协议实现是对的

我用一个本地 WebSocket 客户端（不经过手机）完整跑了一遍：

```
[ws] -> action:0:versionNegotiation?{"versions":[1]}
[ws] <- ack:0:versionNegotiation?{"version":1}
[ws]    negotiated version 1
[ws] -> action:0:sendRequest?{"id":"0001","senderId":"0123456789abcdef",...}
```

帧格式、握手的 `Sec-WebSocket-Accept`、500ms 延时、消息顺序都和
`fb/b.java` + `eb/b.java` + `eb/e.java` 的构造一致。

所以手机那边 `isConnected:0` / 13 秒断开的责任不在帧编码，而在**时序或状态机**。
最可能的解释是：`HttpMessageManager.a()`（`onConnected`）是在 `db.b` 的
**Netty 连接建立回调**里调用的，而我们的 `versionNegotiation` 在握手完成后
500ms 就发了 —— 如果手机的 `HttpP2pClient` 还没把 `HttpMessageManager`
注册成 listener（`registerMessageListener` 在 `onConnectSucess` 之后），
这条就会被丢掉，之后手机在等 `sendRequest` 超时（13s）。

**建议的验证**：把 `--delay` 参数化（默认 1.5s），下次测试时先看手机有没有
`onConnected, isConnected:1`。如果还是 0，就说明它在等别的消息。
我把延时做成可配置：

```sh
python3 tools/oshare_ws.py --port 50856 --delay 2.0
```

---

# 第十六轮：方向搞清楚了——手机上现在是 **HttpP2pClient（接收方）**

把 `ib/b.java` 的 `delay stop server!` 追到了源头：

```java
// ib/b.java:932
public final void h0() {
    this.f15279k.set(true);
    if (this.f15284p == null) this.f15284p = new d();
    this.f15274f.postDelayed(this.f15284p, 2 * 1000);   // 2 秒后
}
// 触发点（onMessageSent 分支，ib/b.java:483）：
if (eb.b.m(eVar)) {                        // action:status
    int type = za.c.x(json, "type");
    if (type == 2) { h0(); return; }       // ★ 服务端发 status:2 → 我们 2 秒后收摊
    if (type == 3 && "user interrupt") { h0(); }
}
```

也就是说：**我们往 `0x9953` 写了 `port`，手机就变成了 `HttpP2pClient`（客户端/接收方）**，
它在等**我们**（`HttpWlanServer` 角色）发 `action:status` 并把文件通过 `/download` 推给它。

这与 `OShareServer.ServerCallback.d(JSONObject)` 的代码一致：

```java
if (oShareServer.f9752m == null)
    oShareServer.f9752m = new ib.b(...);          // HttpP2pClient
oShareServer.f9752m.X(aVar, freq, 0, null);       // connectToServer
```

`ib.b` = **HttpP2pClient**，`fa/p` = **HttpWlanServer**。我们推了 JSON，
所以手机认为**我们是发送方**。

## 这样一来，`versionNegotiation` 的发起方也应该对调

`fa/p.java`（服务端）在 WS 握手 500ms 后发 `action:0:versionNegotiation`；
`ib/b.java`（客户端）收到后回 `ack:...`。

我们现在的 `oshare_ws.py` 正是这么做的，所以**顺序没错**。

手机 13 秒断开前最后两条是：

```
OShare_HttpP2pClient: DeviceProgress: percent = 0
OShare_HttpP2pClient: delay stop server!
```

`DeviceProgress` 来自 `mb.u.a.a(i10, j10)` —— 传输进度回调。
`percent = 0` 说明它认为传输已经开始（但 0%），然后走 `h0()` 的 2 秒延迟收摊。
**`h0()` 也可能不是只有 `action:status` 会调**——`b()`（onClose）里没有，但
`ib/b.java:187` 那段失败处理也走 `f15270b.o()`。

所以最可能的是：**我们回 WS 101 之后，手机把它当成"HTTP 下载端点"用了**，
`HttpMessageManager.onConnected` 把 `isConnected` 置 1（日志打印的是**置 1 之前**的值 0），
然后它尝试 `GET /download?taskId=…`，我们回了 16 字节的 `bytes(range(16))`，
内容不是合法数据流 → 进度 0 → 失败收摊。

## 下一步（具体、可验证）

1. 让 `oshare_ws.py` 把 `/download` 的请求头**完整打印**（现在只打了 path），
   并**故意挂起不响应**，看手机是自己超时还是立刻断——这能区分
   "它没发 download 请求" 和 "它发了但我们回错"。
2. 若它确实发了 `GET /download?taskId=…`，我们就按抓包里的格式回
   `Transfer-Encoding: chunked` + 一段合法的 zip/data 流（`cap_p2p.pcap` 里
   63.8 MB 的那条可以直接拿来对照）。
3. `versionNegotiation` 的 ack 我们本地已经验证过是对的，保持 `--delay 1.5`。

## 当前完整链路状态

```
✅ BLE 发现（0x8881 + 0x6666/0x6667 分包）
✅ 手机连接、读我们 0x9996、写 300 字节分享请求
✅ 读 0x9954 → 写 0x9953（纯写入，无通知、无 CCCD）
✅ P2P GO 协商：关联 + 4 次握手（PSK 生效）
✅ DHCP：手机拿到 192.168.49.81
✅ TLS：wss://192.168.49.1:50856/websocket 握手成功（101）
✅ 我们的 versionNegotiation / sendRequest 帧格式已本地验证
⬜ 手机在 13 秒后放弃，原因待定位（DeviceProgress 0 → delay stop server）
```

这一轮的所有工具都已推到 main：
`tools/oshare_ble.py`、`tools/push_json.py`、`tools/p2p_go.sh`、`tools/oshare_ws.py`。

---

# 手机端回答（第十六轮）：`sendRequest` 的完整字段、以及 `/download` 要回什么

## 1. `action:0:sendRequest?<json>` 的字段（`eb/d.java` 逐字段抄的）

客户端（`ib/b.java`）收到 `sendRequest` 后 `new eb.d(json)`，`i(JSONObject)` 里读的是：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | **taskId**，后面 `GET /download?taskId=<这个值>` 必须一致 |
| `senderId` | string | 发送方设备 id |
| `senderName` | string | 发送方名字（弹窗显示） |
| `fileName` | string | 单文件时的文件名 |
| `mimeType` | string | `*/*` / `image/*` / `video/*` / `vision/*` |
| `fileCount` | int | 文件数 |
| `number` | string | 可选，协议 ≥4 时用（`vision/*` 是 `"图片数+视频数"`） |
| `version` | string | 协议版本 |
| `totalSize` | long | 总字节数（**会跟手机剩余空间比较，不够就回 `status type=3 "no enough space"` 并放弃**） |
| `thumbnail` | string | Base64 缩略图，可空 |
| `thumbnail_width` / `thumbnail_height` | int | 可 0 |
| `wlan` | bool | 走 wlan 时为 true |

## 2. 收到 `sendRequest` 之后手机做了什么（这很可能就是你卡住的地方）

```java
// ib/b.java  d(db.c) → eb.b.k(eVar) 分支
b.this.D = new eb.d(eVar.c());
if (freeSpace < D.k() /*totalSize*/) { send status(type=3,"no enough space"); return; }
f15270b.x(1, 0); f15270b.x(3, totalSize);
state = TRANSIT_WAIT;
gb.a.d().g(D.h());          // 记下 taskId
m(D.j() /*thumbnail*/, reqId);
```

而 `m(str, i10)`：

```java
ReceiveCommonDialogActivity.f1(ctx, D.d(), D.c(), totalSize, D.f(), D.g(), null);  // ★ 弹接收框
eb.c.i().m(eb.b.a(i10, "sendRequest"));                                            // 回 ack
```

**也就是说：手机收到 sendRequest 会弹"是否接收"的对话框，然后回 `ack:sendRequest`，
之后要等用户在手机上点“接收”，才会去 `GET /download?taskId=…`。**
你看到的 `DeviceProgress: percent = 0` 是任务建好后的进度追踪，属于正常现象。

所以先确认两件事：
* 手机屏幕上有没有弹出接收框？（如果没有，说明 `sendRequest` 被丢掉了或 `eb.d` 解析失败）
* 弹框后有没有人去点？点完日志里会出现下载相关行。

## 3. `delay stop server!` 是被我们自己的 `status` 触发的

```java
// ib/b.java onMessageSent → eb.b.m(eVar)（即 action:status）
int type = za.c.x(json, "type");
if (type == 2) { h0(); return; }            // ★ h0(): 2 秒后收摊
if (type == 3 && "user interrupt".equals(reason)) h0();
```

**服务端的 `status` 里 `type == 2` 表示“结束/失败”**，一旦发出，手机 2 秒后主动断开。
所以：`status` 只在真正传完之后发一次，中间不要发 `type=2`。
`DeviceProgress 0 → delay stop server!` 这个组合，就是"我们提前发了 type=2"或者
"手机自己判定失败后走的同一条收尾"。

## 4. `/download` 要回什么

`z9/a.java` 里服务端的做法：

```java
response.headers().set("Server", "VS-HTTP");
response.headers().set(TRANSFER_ENCODING, CHUNKED);
response.headers().set("Content-Type", "application/zip");
response.headers().set("Content-Disposition", "attachment; filename=\"files.zip\"");
writeAndFlush(new HttpChunkedInput(new ea.a(...)));   // ea.a = ChunkedZipOutputStream
```

`ea/a.java` 用的是标准 `java.util.zip.ZipOutputStream` + `ZipEntry`，
所以**回一个正常的 ZIP 流就行**（chunked），手机解压后落到 `/sdcard/Download/realme Share/`。
不需要自定义封装。

请求形如 `GET /download?taskId=<sendRequest 里的 id>`；
单文件时还会带 `&fileId=…`（`ra/b.java`）。

## 5. 建议的验证顺序

1. 发 `sendRequest`（字段按上表，`totalSize` 别写 0，`fileCount` 与实际一致，`id` 用固定值）；
2. **看手机屏幕**：有没有接收框 → 点接收；
3. 点完之后手机应该发 `ack:sendRequest` 并 `GET /download?taskId=<id>`；
4. 我们回 chunked zip（先用一个几百字节的 zip 试）；
5. 过程中**不要发 `status type=2`**，传完了再发。

如果第 3 步的 `GET /download` 一直不来，那就是手机还在等用户操作（或者弹框被别的界面压掉了），
可以把 `ReceiveCommonDialogActivity` 拉到前台看：`adb shell dumpsys activity activities | grep -i receive`。
