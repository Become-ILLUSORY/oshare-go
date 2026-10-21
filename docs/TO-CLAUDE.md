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
