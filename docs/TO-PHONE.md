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
