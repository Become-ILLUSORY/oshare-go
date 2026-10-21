# 给下一位接手的 Claude（或任何人）

*(来自上一轮对话的接手信息，2026-10-21 晚)*

## 你手上的现状

```
✅ 手机「其他设备」能看到 LinuxBox（已截图确认）
✅ 手机连上、读 0x9996、往我们 0x9985 写了 74 字节的分享请求
❌ 卡在：手机不写 0x9985 的 CCC 描述符 → bluez 不发我们的通知 →
   手机提示「发送超时，对方未确认」
```

## 我这一轮改了什么（已推 main，提交 见 git log）

1. **换方向做 ACK**。ACK 不是通知，是**写**。
   `ble/impl/w.java` 的 `onCharacteristicWriteRequest`：

   ```java
   // uuid == 0x9985
   byte b = value[0];
   if (b == 1) o(address, value);      // 允许
   else        k(address, char, off);  // 拒绝/取消
   ```

   所以手机在等我们**作为 GATT client 连回它、往它的 0x9985 写一个 `0x01`**。
   `tools/oshare_ble.py` 里新增 `ack_peer()`：任何一次来自手机的写入之后
   （带 `--ack` 默认开启），自动 Connect 该设备 → 在 GATT 树里找
   `00009985-…` → `WriteValue(b"\x01")`，失败会重试 5 次（每次 700 ms），
   因为 BlueZ 连上后属性树是异步出现的。

2. **别指望 CCCD**。ColorOS 的客户端 `onDescriptorWrite` 是空实现，只调用
   `setCharacteristicNotification()` 本地置位——它永远不写 CCCD，BlueZ 因此
   永远认为我们"没人订阅"，`PropertiesChanged` 不会发出去。这就是你看到的
   现象的根因，不是 bug。

3. **文档补了三块**（`docs/BLE.md` 末尾）：
   - ACK 语义（写 0x01，不是通知）
   - **Android GATT 缓存陷阱**：改了服务结构后手机会一直用旧的属性表，
     必须 `adb shell su -c 'svc bluetooth disable; sleep 2; svc bluetooth enable'`
     （或换设备地址/解除配对）才会重新发现。你现在看到的"0x9999 下没有 0x2902"
     很可能是这个缓存。
   - 手机自己服务端用的**私有描述符**（不是 0x2902）：0x9998→0x9994、
     0x9996→0x9993、0x9995→0x9992、0x9990→0x9991、0x9989→0x9988、
     0x9986→0x9987。想更贴近原厂行为可以把它们也镜像上去（可选）。

## 你接下来按这个顺序试

```sh
git pull

# 0) 清掉手机的 GATT 缓存（关键！先用旧代码测过就要清）
adb shell su -c 'svc bluetooth disable; sleep 2; svc bluetooth enable'

# 1) 一边开日志（另开一个终端，看手机到底在等什么）
adb logcat -s BleServer:V BleClient:V BleServer_iPhone:V OShare_:V

# 2) 起服务端
sudo python3 tools/oshare_ble.py serve --name LinuxBox
```

手机点「LinuxBox」后，终端应该出现：

```
[gatt] write WPS_RESULT/ACK_DIALOG/… (74 bytes)
[ack] peer device /org/bluez/hci0/dev_XX_XX_…
[ack] connected to peer, scanning its GATT tree for 0x9985
[ack] writing 01 to /org/bluez/hci0/dev_XX_…/service…/char…
[ack] peer acknowledged (write ok)
```

手机侧对应会打印 `BleServer onCharacteristicWriteRequest BLE_ACK_DIALOG flag: 1`。

### 如果还是不通，按可能性排

| 现象 | 下一步 |
|---|---|
| `[ack] connect failed` | 手机不接受反连。改用通知路线：强行让 bluez 认为已订阅——目前没有干净办法，可考虑给特征加标准 CCCD 并让手机先写一次（清缓存后再试） |
| 连上了但 `0x9985 not found` | 手机的 GATT 树里没有暴露自身服务给外部 client（它只在被连接时把服务给"对端"），此时必须走通知；可尝试在广告里带上服务 UUID 0x9999，让手机把我们也当 server |
| ACK 写成功但手机仍超时 | 说明 74 字节请求里要求的不是 0x9985 的 verdict，去看 `BleServer` 日志下一句；也可能是要写 `0x9986`（WPS/GO 信息）而不是 ACK |
| 手机根本不连 | 检查广告里 0x8881 是否第一个 UUID、能力位 0x05 |

## 之后的路（BLE 打通后）

1. 拿到 P2P/热点凭据（`0x9986` 的 `mac&band&type&ssid&psk…` 或 `0x9953` 的 JSON 端口）；
2. 加入 Wi-Fi Direct 组；
3. 用 `transfer/`（已有 TLS 客户端骨架）打 `https://<phone>:<port>/download?taskId=…`；
4. 详细协议见 `PROTOCOL.md` 与 `docs/HANDOFF.md` 第 5、9 节。

## 提醒

- 本仓库 Go 侧 `go test ./...` 全绿，改 Python 工具不影响 CI。
- 手机上抓包工具我留在 `/data/local/tmp/pcapcap`（AF_PACKET → pcap），
  需要复现传输层的证据时可以直接用。
- 抓包样本在 release `captures-2026-10-21`。

---

# 第二轮：回答你的四个问题（附代码依据）

> 结论先说：**「反连写 0x01」这条路是错的**，不是方向问题，是语义问题。
> `0x9985` 上的写入在手机端被当成**缩略图分片**处理，不是"允许"。

## Q1 `w$e.onCharacteristicWriteRequest` 的开头（jadx 反编译失败，用 `--show-bad-code` 拿到了）

```java
q0.c("BleServer", "onCharacteristicWriteRequest responseNeeded: " + z11 + " offsetValue: " + i11);
if (mGattServer == null && !DcpManager.r().p()) return;      // 不回 response
if (z0(dev)) { q0.o(..., "device is empty!"); return; }        // 不回 response
String address = dev.getAddress();
m mVar = mDeviceInfoMap.get(address);                          // ← 关键
if (mVar == null || !mVar.a()) {                               // a() == hasRead
    q0.o("BleServer", "can not get deviceinfo! " + address);
    return;                                                    // ★ 不回 response → 你看到的 ATT 超时
}
if (P0(dev)) { q0.o(..., "write device not same read device, return"); return; }
char.setValue(value);
if (Q0(mVar)) {                                                // Q0 = (mVar.b() == 1)  transferType == 1
    if (uuid == 0x9995) { ... 取消 ... }
    else if (uuid == 0x9984) { ... log ... }
    else if (uuid == 0x9990) { ... start ... }
    else if (uuid == 0x9989) { ... }
    else if (uuid == 0x9986) { /* START_WITH_WPS_INFO，长度前缀分片，见下 */ }
    else if (uuid == 0x9985) {
        byte b = value[0];
        if (b == 1) o(address, value);      // ← 注意：o() 不是"允许"
        else        k(address, char, off);
    }
    ...
}
```

**`o(String, byte[])` 的真实实现**（同一文件 948 行，日志里叫 `handlePreviewData`）：

```java
public final void o(String str, byte[] bArr) {
    if (bArr.length < 5) return;
    int dataOffset = utils.l.a(bArr, 1);            // 第 2..5 字节 = 偏移
    aVar.f(dataOffset, Arrays.copyOfRange(bArr, 5, bArr.length));  // 第 6 字节起 = 数据
    ... 拼完 → z(aVar.c(), completeData);            // 拼成一张预览图
}
```

所以 **0x9985 的写入 = 缩略图分片传输**（首字节 1、偏移 int32、之后是图像数据），
不是"允许/拒绝"。写 0x01 除了会被早退，语义上也不对。

### 手机愿意回 response 的前提（也是你超时的直接原因）

1. `mDeviceInfoMap[addr]` 存在；
2. `mVar.a()`（= hasRead）为 **true** —— 只有在**对端读过手机的 `0x9996`** 时才会被置位
   （`onCharacteristicReadRequest` 里 `mVar.c(true); mVar.d(1)`）；
3. `P0(dev)` 为 false —— 写入的设备必须等于最后一次读的设备；
4. `Q0(mVar)`：`transferType == 1`（同样是读 0x9996 设的）。

也就是说：**先读 0x9996，才可能写成功**。你直接写 0x9985，手机第一条就 return，ATT 自然超时。

## Q2 `w.z0(BluetoothDevice)`

```java
public final boolean z0(BluetoothDevice dev) {
    if (dev != null && !TextUtils.isEmpty(dev.getAddress())) return false;
    q0.o("BleServer", " device is null, return");
    return true;
}
```
只是个判空，没有白名单逻辑。

## Q3 `j.onDescriptorWrite` 是不是空实现

是。整段只有一行日志：

```java
public void onDescriptorWrite(BluetoothGatt gatt, BluetoothGattDescriptor d, int status) {
    j.this.f9873a.a("onDescriptorWrite");
}
```

但注意：**手机是会写 CCCD 的**，只是只在 iOS 分支里写：
`j.U1()` 里 `char.getDescriptor(k9.c.D /* 0x2902 */)` → `descriptor.setValue(ENABLE_NOTIFICATION_VALUE)`
→ `gatt.writeDescriptor(descriptor)`。联盟/P2P 分支（`S1()`/`T1()`）只调
`setCharacteristicNotification(char, true)`，不写描述符。

这印证了：**对端（小米/我们）必须自己发通知而不依赖 CCCD**——Android 作为 GATT server
时 `notifyCharacteristicChanged()` 不看 CCCD，而 BlueZ 看。

## Q4 手机端 GATT 缓存

现在 adb 掉了（无线调试断连），我这边连不上手机，没法立刻做。你可以在笔记本上直接做：

```sh
adb shell su -c 'rm -f /data/misc/bluetooth/gatt_*'
adb shell su -c 'svc bluetooth disable; sleep 2; svc bluetooth enable'
# 再连一次，然后
adb shell su -c 'ls -l /data/misc/bluetooth/ | grep -i gatt'
```

判定标准：
* 缓存文件里 `0x9999` 服务下**出现 `2902`** → 说明 BlueZ 其实建了 CCCD，问题在手机没去写它；
* 仍然没有 → BlueZ 根本没建，需要我们在 GATT 里显式声明一个 `0x2902` 描述符
  （BlueZ 对 notify 特征会自动建，但如果你看到空隙 0x2db 却没属性，可能就是"建了但属性表没列出来"，
  用 `btgatt-client -d` 或 nRF Connect 从第二台设备看一眼最准）。

## 那么真正的 ACK 是什么（这是我上一轮搞错的地方，纠正）

手机当**发送方**时（当前场景），它等的是 **0x9985 上的通知**，内容是
**Base64 的 AP/组信息 JSON**：

```java
// j.onCharacteristicChanged, uuid == 0x9985 (k9.c.f19000a)
String s = new String(Base64.decode(value, 0), UTF_8);
listener.b(s);                      // BleClientEventHandler.onBleAckResult

// 构造方：w.i1(softap.d.a(cfg, is5G, P, R))
// za.c.p(...) / za.c.q(...) 两个 builder：
//   {"ssid": "...", "psk": "...", "ip": "...", "port": "...", "isFast": 0}
//   {"ssid": "...", "psk": "...", "ip": "...", "port": "...", "key": "...", "isFast": 0}
```

即：**拥有 AP/GO 的那一端把 `{ssid,psk,ip,port[,key],isFast}` 用 Base64 通知给对端**。
在我们这个方向（手机发给我），手机自己是 GO，所以它应该把这段发**给我们**；
它没发，是因为它还在等我们先把 `0x9989`（REQUEST_GO）播上去。

## 所以真正卡住的是「我们发不出通知」

顺序（代码推导）：

```
我们 notify 0x9989 ``<mac><3>&<freq>&<str2>&<str3>[&<port>]``   ← 总长 ≥45 字节，按 22 字节切片，最多 3 片
   ↓ 手机 j 收到后累加，够 45 字节触发
手机创建 GO/AP
   ↓
手机 notify 0x9985  Base64({"ssid","psk","ip","port",...})
   ↓ 我们收到，加入组，然后 TLS + GET /download?taskId=
```

而第一步就是被 BlueZ 的 CCCD 门槛挡住的。

### 三条可选突围路线（按性价比）

1. **试 `AcquireNotify`**（5 分钟能验证）：
   ```python
   fd, mtu = bus.call(char_path, "org.bluez.GattCharacteristic1", "AcquireNotify", ...)
   os.write(fd, payload)
   ```
   BlueZ 5.62+ 有这个 API，设计上就是给高频通知用的；如果它不检查 CCC 状态，问题直接解决。
2. **BlueZ 打补丁**：CachyOS 是 Arch 系，`asp checkout bluez` 改
   `src/gatt-database.c` 里 notify 的 CCC 判断（或把 CCC 默认值设为 0x0001），
   本地 `makepkg -si`。一次性把这条路打开。
3. **ESP32 当 peripheral**（NimBLE 里 `ble_gatts_notify_custom()` 不看 CCCD），
   串口/UART 把数据转给笔记本。稳定但要多一块硬件。

我这边会继续补 BLE 抓包（等 adb 恢复），有结果直接推文档。

---

# 第三轮：回答手机端三问 + 方向修正

## Q1「手机发送」方向到底等不等我们的 0x9989？

**不等。反过来：是手机自己建 GO，然后把它自己的组信息用 0x9989 通知我们。**

触发点在 `com/oplus/oshare/OShareServer.java:484`：

```java
// OShareServer$ServerCallback.b(com.oplus.oshare.ble.impl.w)   ← 名字就是 onCreateGroupOwner
q0.c("OShareServer", "onCreateGroupOwner");
...
wVar.f1(oShareServer.A, oShareServer.f9759x, oShareServer.f9760y, oShareServer.f9761z);
```

而 `w.f1()` 就是：

```java
if (this.C < 3) {                       // 对端协议版本 < 3
    y0(H0(str, freq), k9.c.f19004e);    // 0x9989：短格式
    return;
}
String s = new String(I0(str, freq, str2, str3), UTF_8);   // 长格式
for (int i = 0; i < 3; i++) {           // 按 22 字节切片，最多 3 片
    y0(s.substring(i*22, min((i+1)*22, len)).getBytes(), k9.c.f19004e);
}
```

所以链路是：

```
手机（发送方/GO）──notify 0x9989──> 我们      组信息 "<mac>3&<freq>&<str2>&<str3>[&<port>]"
手机 ──notify 0x9985──> 我们                  Base64({"ssid","psk","ip","port"[,"key"],"isFast"})
我们加入组 → TLS 连接 → GET /download?taskId=
```

**结论：我们一条通知都不用发**，只需要**收**手机的两条通知。这就绕开了 BlueZ 服务端的 CCCD 门槛。

> 反方向（Linux 发、手机收）才需要我们去 notify 0x9989——那时手机是 client，等我们的 REQUEST_GO。

## Q2 0x9996 里 `MFkwEwYHKoZIzj0...` 是什么

是 **DER 编码的 EC P-256 公钥**（SubjectPublicKeyInfo）。
`MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgA…` 这段前缀就是 `prime256v1` 的固定头，后面 64 字节是 X||Y。

用途在 `ble/impl/j.java` 的 0x9996 解析里：

```java
String[] f = value.split("&");
if (f.length > 2) { R = f[2]; task.H0(parseInt(f[1]) >= 3); }     // f[1]=协议版本, f[2]=公钥
if (f.length > 4)  randomPort = "1".equals(f[4]);
if (f.length > 5) {
    JSONObject o = new JSONObject(f[5]);          // {"info","salt","sign"}
    if (b0.y(f[2], o["sign"], o["salt"], o["info"]))  P1();   // 验签通过 → 继续
    else                                              c();    // 失败 → 中止
} else {
    P1();                                                     // 没给 JSON 就直接放行
}
```

所以：

* **字段格式**：`<state?>&<version>&<EC 公钥 base64>&<…>&<randomPort flag>&<{"info","salt","sign"}>`
* **验签**：用 f[2] 里的 EC 公钥校验 `info|salt` 的 ECDSA 签名（`utils/b0.y`）。
* 你们现在回 `{"state":0,"mac":…,"key":""}` 只说 6 个字段里最后一个是那个 JSON；
  手机日志 `Queried the status info is: 0` 说明它走了"没给公钥/没给 JSON"的宽松分支（`P1()`），
  这在**手机当发送方**时是够的——它不需要验我们的身份，只要我们不拒绝。
* 只有当**我们当发送方、手机当接收方**时，这个公钥/签名才会被真正校验；届时需要：
  生成 EC P-256 密钥对 → 公钥 base64(DER SPKI) 放进 f[2] → `sign = ECDSA-SHA256(info + salt)`（具体拼接看 `b0.y` 实现）。

## Q3 0xbb15（DCP）里那套是不是同一份逻辑

不是同一套用途，但 UUID 有重叠，所以**按 UUID 找特征会误伤**（你们踩到了）：

* `0x9999` / `0x9955` = OShare 分享服务（`ble/impl/w.G0()` 就是它，逻辑全在上面）。
* `0xbb15` = PantaConnect SDK 的 DCP 服务（`GattServerEventType.SERVICE_UUID`），
  给"联系人/近场"用；`w.s1()` 里它只是 **fallback**：
  ```java
  BluetoothGattService s = gatt.getService(k9.c.f19006g /* 0x9999 */);
  if (s != null) return s;
  return gatt.getService(GattServerEventType.Companion.getSERVICE_UUID());  // 0xbb15
  ```
  也就是说：手机优先用 0x9999，找不到才退回 0xbb15。**我们只要 0x9999 那份，DCP 那份可以忽略**
  （如果 BlueZ 上两份都存在，按 `service/char` 的 handle 而不是 UUID 去选）。

## 下一步（按你说的"只能是通知"这个结论）

既然我们只需要**收**通知，问题就变成 BlueZ 客户端侧的 StartNotify 能不能在对方没有 0x2902 的情况下生效：

1. 直接对手机的 `0x9989` 调 `StartNotify`，**即使报错也继续等一下**，
   看 `PropertiesChanged` 有没有把手机的 REQUEST_GO 通知送上来
   （BlueZ 的 `characteristic_start_notify()` 是先置 `notifying = true` 再写 CCCD，
   写 CCCD 失败不一定会回滚，值得一试）。
2. 不行就先手写手机的自定义描述符 `0x9988`（0x9989 的描述符）= `01 00`，再 StartNotify。
3. 再不行就 `btmon` 看手机到底有没有真的把 notification 发出来（Android 侧不看 CCCD，应该会发）。
   如果 btmon 里看得到 ATT Handle Value Notification、但 BlueZ D-Bus 不给我们，
   那就是 BlueZ 客户端过滤，需要客户端补丁或换 ESP32 当**服务端**（我们做 central 时不需要它）。

我这边继续等 adb 恢复，然后按 Q1 的链路把手机的 0x9989/0x9985 通知内容抓出来（HCI 里是加密的，
但可以用 logcat 的 `BleClient onCharacteristicChanged` 直接读到明文）。

---

# 第四轮：两个问题的答案 + 一条可能绕开通知的新路

## Q1 `onDescriptorWriteRequest` 做了什么？（`--show-bad-code` 完整版）

```java
public void onDescriptorWriteRequest(BluetoothDevice dev, int requestId,
                                     BluetoothGattDescriptor desc,
                                     boolean preparedWrite, boolean responseNeeded,
                                     int offset, byte[] value) {
    if (mGattServer == null) { log("GattServer is null, return"); return; }
    desc.setValue(value);
    if (responseNeeded) mGattServer.sendResponse(dev, requestId, 0, 0, null);
}
```

**就这么两行：存值 + 回 response。没有任何标志位，也没有任何 gating。**
写 `0x9988` 对 `y0()` 发不发通知**完全没有影响**——你的实验结论（写了也没用）和代码一致。

## Q2 手机给真机对端发 0x9989 时 `notifyCharacteristicChanged()` 成功吗？

先看 `y0()`：

```java
public final void y0(byte[] data, UUID uuid) {
    BluetoothGattCharacteristic c = mService.getCharacteristic(uuid);
    if (c == null || mSelectDevice == null) return;
    c.setValue(data);
    try {
        mGattServer.notifyCharacteristicChanged(mSelectDevice, c, false);  // ← 返回值被丢弃
    } catch (Exception e) { log(e); }
}
```

**返回值被丢掉**，所以代码层面判断不了。但有两个事实可以反推：

1. OPPO ↔ Xiaomi 那两次真实传输**是成功的**，而 0x9989 的 REQUEST_GO 是这条链上唯一的组信息通道
   ⇒ 安卓侧的 `notifyCharacteristicChanged()` 对这两个"只声明了 read|write"的特征**确实发出去了**
   （Android 的 GATT server 发送通知时不检查属性位）。
2. BlueZ 拒绝的原因不是"没有 CCCD"，而是 `StartNotify` 一开始就检查**特征属性位**：

   ```c
   // bluez/src/gatt-client.c, characteristic_start_notify()
   if (!(chrc->props & (BT_ATT_CHR_PROP_NOTIFY | BT_ATT_CHR_PROP_INDICATE)))
           return btd_error_not_supported(msg);
   ```

   手机把 0x9989/0x9985 声明成 read|write，所以 BlueZ 连 CCCD 那步都没走到就 NotSupported 了。

**结论：协议没坏，是 BlueZ 的策略在挡。** 客户端想做就要么打补丁（把这个属性检查去掉，
并在没有 CCCD 时也置 `notifying = true`），要么换非 BlueZ 的 peripheral/central。

## 但先别急着打补丁——0x9996 是可以「拉」的

`0x9996` 的值不是静态的，是**每次读的时候现算**（`w.java` 读处理里的 task 回调，约 1214 行）：

```java
sb2.append(x.w(ctx));                  // 本机 Wi-Fi MAC
sb2.append("&").append(3);             // 协议版本 3
if (G /*MTU 已协商*/ || DcpManager.p()) {
    String pub = b0.o().q(null);       // "getServerPublicCode"
    sb2.append("&").append(pub);       // 字段 2：EC P-256 公钥（DER SPKI → base64）
    sb2.append("&").append("1");       // 字段 3
    sb2.append("&").append("1");       // 字段 4 = 随机端口标志
    g2.b w = b0.w(pub);                // ★ 用这个公钥做 ECIES 容器
    if (w != null)
        sb2.append("&").append(za.c.d(b64(w.c()), b64(w.b()), b64(w.a())));  // {"info","salt","sign"}
}
```

而 `b0.w(pubkey)`：

```java
f2.a a = new f2.a();
a.b(params);                 // 2 字节随机 + "share"
return a.c(pubkeyBytes);     // ECDH → KDF → 加密，返回 {a,b,c} = {sign, salt, info}
```

`f2.a.c()` 内部走 `d2.a`/`d2.b`/`i2.a`（SHA-256/HMAC + 密钥派生，部分调用会抛
`TAInterfaceException`，说明有 TEE 参与，但算法原语都在这三个类里）。

**关键推断**：`q(null)` 返回的是 "server public code"——如果它在读了对端的 0x9996 之后被缓存成
**对端的公钥**，那么这段 `info` 就是**加密给对端**的，对端用私钥就能解出 GO/AP 凭据。
那就完全不需要任何通知：**读两次 0x9996 就够**（第一次让手机看到我们的公钥，第二次拿到加密凭据）。

### 建议的验证顺序

1. Linux 端生成 EC P-256 密钥对，把公钥 base64(DER SPKI) 放进我们 0x9996 的字段 2：
   `<wifi-mac>&3&<pub>&1&1`（字段 5 的签名块可以先不放，`f.length<=5` 会走 `P1()` 放行）。
2. 让手机读一次（它已经在读了），**再读手机 0x9996**：
   * 如果 `info` 变了 / 能解出 `{"ssid","psk","ip","port"...}` → 通知这条路可以彻底放弃；
   * 如果 `info` 仍是给手机自己公钥加密的 → 再看 `b0.o().q(null)` 到底缓存谁的公钥（
     `l(String)` / `f12019a` map 是 keyed by taskId，值得单独看一眼）。
3. 同时打开 `adb logcat -s EncryptOrDecryptUtil:V BleServer:V`，手机在读写 0x9996 时会打印
   `getServerPublicCode` 相关信息，能直接看出它缓存的是谁的公钥。

如果 2 成立，我们就能只用 **read** 完成整个握手，BlueZ 的 CCCD/属性检查问题一笔勾销。
