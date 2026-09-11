# Phase 0 查詢 — 空秤讀數在歷史上有多密?

_計畫見 [scale-zero-drift-plan.md](./scale-zero-drift-plan.md)。這三個查詢回答一件事:**零點漂移的修正
能不能回溯套用到歷史資料**,還是只能往前。_

在 broker host 上跑:

```sh
cd broker
./influx-q.sh -f /path/to/q1.flux      # 或 ./influx-q.sh '<貼上查詢>'
```

三個查詢都遵守 `QUERYING.md` 的陷阱:**先 filter `_field`**(陷阱 2 —— 不然
`plant_weight` 裡的 string 欄位會讓 group 回傳空的,而那看起來和「沒有資料」一樣)、
**絕對時間邊界**(相對視窗會切掉第一天,incidents README 明文)、
**`option location` 只影響日界計算,輸出仍是 UTC**(陷阱 1,`influx-q.sh` 會幫你轉)。

---

## Q1 — 覆蓋率:有多少個秤重日完全沒有空秤讀數

**這是決定性的一題。** 若絕大多數秤重日都有空秤讀數 → 歷史可回溯修正,整個計畫
的量級不同。

```flux
import "timezone"
option location = timezone.location(name: "Asia/Taipei")

from(bucket: "sensors")
  |> range(start: 2026-07-13T00:00:00Z, stop: 2026-09-12T00:00:00Z)
  // 陷阱 2:先選 _field,否則 uid(string)會讓後面的 group 回空
  |> filter(fn: (r) => r._measurement == "plant_weight" and r._field == "weight_g")
  |> group()
  |> window(every: 1d)
  |> reduce(
       identity: {n_ok: 0, n_empty: 0, n_other: 0},
       fn: (r, accumulator) => ({
         n_ok:    accumulator.n_ok    + (if r.quality == "ok"    then 1 else 0),
         n_empty: accumulator.n_empty + (if r.quality == "empty" then 1 else 0),
         n_other: accumulator.n_other + (if r.quality != "ok" and r.quality != "empty"
                                         then 1 else 0),
       }))
  |> duplicate(column: "_stop", as: "_time")
  |> window(every: inf)
  |> sort(columns: ["_time"])
```

**怎麼讀**

- `n_ok > 0 且 n_empty == 0` 的日子 = **那天的零點無法回溯重建**
- 這種日子佔比小 → 回溯修正可行(缺的那幾天標成未修正即可)
- 佔比大 → 修正只能往前,而 Phase 1 的實體參考物就是唯一的路
- `n_other > 0` 代表還有我不知道的 quality 值(例如 `mark-weight.sh` 標記過的),
  要先問清楚那是什麼再往下

---

## Q2 — 形狀:那四個異常場次當天到底長什麼樣

incident 點名了四個「集體同向變重」的場次。**與其抽象地問覆蓋率,不如直接看這四天。**
它們是修正最該生效的地方,也是 Phase 3 驗證的檢查點。

```flux
import "timezone"
option location = timezone.location(name: "Asia/Taipei")

from(bucket: "sensors")
  |> range(start: 2026-08-25T00:00:00Z, stop: 2026-08-26T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "plant_weight" and r._field == "weight_g")
  |> keep(columns: ["_time", "_value", "quality", "plant_id"])
  |> group()
  |> sort(columns: ["_time"])
```

把 `range` 換成另外三段各跑一次:

```
2026-08-08T00:00:00Z → 2026-08-11T00:00:00Z
2026-08-29T00:00:00Z → 2026-08-31T00:00:00Z
2026-09-05T00:00:00Z → 2026-09-07T00:00:00Z
```

**怎麼讀**

- 空秤讀數是**包夾**植物讀數(前後都有),還是只有開頭/結尾一筆,還是完全沒有?
- 包夾 → 可以內插出該場次的零點,修正品質最好
- 只有一筆 → 只能當常數偏移用
- 完全沒有 → 這四個最需要修的場次反而修不了,那是很重要的壞消息

08-25 那天要特別看:incident 說「同一天隔 9 小時、12 株全部變重」,所以那天應該
看得到**兩組**植物讀數。兩組各自的零點若不同,差值就是我們要修掉的東西。

---

## Q3 — 天花板:零點量測自己有多吵

**這一題決定修正最好能做到多好。** 若同一場次內多筆空秤讀數自己就散好幾克,
那修正的殘差不可能小於那個數 —— 整個方案的上限就在這裡。

```flux
import "timezone"
option location = timezone.location(name: "Asia/Taipei")

from(bucket: "sensors")
  |> range(start: 2026-07-13T00:00:00Z, stop: 2026-09-12T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "plant_weight" and r._field == "weight_g")
  |> filter(fn: (r) => r.quality == "empty")
  |> group()
  |> window(every: 1d)
  |> reduce(
       identity: {n: 0, lo: 100000.0, hi: -100000.0, sum: 0.0},
       fn: (r, accumulator) => ({
         n:   accumulator.n + 1,
         lo:  if r._value < accumulator.lo then r._value else accumulator.lo,
         hi:  if r._value > accumulator.hi then r._value else accumulator.hi,
         sum: accumulator.sum + r._value,
       }))
  |> filter(fn: (r) => r.n > 1)          // 只有一筆的日子談不上離散度
  |> map(fn: (r) => ({ r with spread: r.hi - r.lo, mean: r.sum / float(v: r.n) }))
  |> duplicate(column: "_stop", as: "_time")
  |> window(every: inf)
  |> sort(columns: ["_time"])
```

**怎麼讀**

- `spread` 的典型值就是**修正的殘差下限**
- 若典型 `spread` ≈ 0.1 g(站台已知的場內重複性)→ 零點量得很準,修正可以做到很好
- 若典型 `spread` 有好幾克 → **零點量測本身就不穩**,修正能消掉的遠少於預期,
  而且要先問為什麼(秤上有東西?溫度在該場次內就在變?)
- `mean` 跨日的變化範圍應該重現 incident 量到的 **−10.2 ~ +11.2 g** —— 若對不上,
  代表我這個查詢和當初那個算的不是同一件事,**先查清楚再繼續**

---

## 三題一起回答什麼

| 結果 | 結論 |
|---|---|
| Q1 覆蓋高 + Q3 spread 小 | **回溯修正可行**,而且效果好 —— 所有歷史的 `used%` 一次變可信 |
| Q1 覆蓋高 + Q3 spread 大 | 零點量測本身不可靠,先查為什麼,別急著套修正 |
| Q1 覆蓋低 | 只能往前修,Phase 1 的實體參考物是唯一的路 |
| Q2 四個異常場次沒有空秤讀數 | 最該被修的場次修不了 —— 驗證要改用別的檢查點 |

跑完把輸出貼回來,我接著寫 Phase 2 的修正查詢。
