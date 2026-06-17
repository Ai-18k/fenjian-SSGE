package kaoman.test5;

import com.alibaba.fastjson2.JSONObject;
import kaoman.bean.Box;
import kaoman.bean.BoxConfig;
import kaoman.bean.Fish;
import lombok.extern.slf4j.Slf4j;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.*;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 进行10顿模拟
 * 装箱逻辑如果箱子装到9条发现可入鱼规格没有能够满足条件的箱子直接进入failBoxList
 *
 */
@Slf4j
public class ImprovedFishBoxing501 {

//    private static int limitWeight = (10 * 1000 * 1000 -100*1000 );
    private static int limitWeight = (4 * 1000 * 1000 );

    private static final int MAX_WEIGHT = 5030;
    private static final int MIN_WEIGHT = 4980;

    private static int BUFFER_SIZE_LIMIT = 90;//缓存池大小限制
    private static int FISH_MAX_ROUND = 480;//超时鱼轮次

    private static final int CACHE_BOX_PER_SPEC = 1; // 每个规格4个暂存箱
    private static int BOX_TYPE = 2; // 1-装n-1 2-装一半 3-不装
//    private static double BOX_RATE = 0.5;

    private static Map<Integer,Fish> outTimeFish_180 = new HashMap<>();
    private static Map<Integer,Fish> outTimeFish_240 = new HashMap<>();
    private static Map<Integer,Fish> outTimeFish_300 = new HashMap<>();
    private static Map<Integer,Fish> outTimeFish_420 = new HashMap<>();
    private static Map<Integer,Fish> outTimeFish_540 = new HashMap<>();
    private static Map<Integer,Fish> outTimeFish_600 = new HashMap<>();
    private static Map<Integer,Fish> outTimeFish_660 = new HashMap<>();

    private static final List<Integer> outTimeFishSize_180 = new ArrayList<>();
    private static final List<Integer> outTimeFishSize_240 = new ArrayList<>();
    private static final List<Integer> outTimeFishSize_300 = new ArrayList<>();
    private static final List<Integer> outTimeFishSize_420 = new ArrayList<>();
    private static final List<Integer> outTimeFishSize_540 = new ArrayList<>();
    private static final List<Integer> outTimeFishSize_600 = new ArrayList<>();
    private static final List<Integer> outTimeFishSize_660 = new ArrayList<>();

    private static int FISH_MIN_WEIGHT;
    private static int FISH_MAX_WEIGHT;

    // 缓冲池：按规格分组，每个规格内部保持插入顺序（FIFO）
    private static final Map<String, List<Fish>> bufferMap = new LinkedHashMap<>();
    private static int totalBuffered = 0;

    // 回流转存计数
//    private static int reflowCount = 0;
    private static List<Fish> reflowFish = new ArrayList<>();

    // 暂存箱：每个规格 CACHE_BOX_PER_SPEC 个箱子
    // cacheBoxes[specIndex][boxIndex]
    private static List<Fish>[][] cacheBoxes;
    private static int[][] sumWeights;
    private static BoxConfig[] configs; // 6个规格配置
    private static int[] boxThreshold;  // 每个规格暂存箱的条数阈值（达到后不再接收新鱼）

    // 上次无匹配缓存（避免短时间内重复搜索）
    private static long[] lastNoMatchTime;
    private static int[] lastNoMatchHash;

    private static int FISH_SIZE = 0;
    private static int MAX_BUFFER_SIZE = 0;
    private static int BOXED_FISH_COUNT = 0; // 已装箱鱼总数

    private static String stopReason = null;
    private static final List<Box> boxList = new ArrayList<>();
    private static final List<Box> failBoxList = new ArrayList<>();

    private static int totalFishWeight = 0;

    private static Map<Integer, List<int[]>> errorInterval;
    private static int[] minPossibleNextWeight;
    private static int calculateSize = 0;
    private static boolean useNew = false;

    private static final List<String> result = new ArrayList<>();
    private static final List<Integer> calSize = new ArrayList<>();
    private static final List<Integer> totalSize = new ArrayList<>();
    private static final List<Integer> subSize = new ArrayList<>();
    private static final List<BigDecimal> remaining = new ArrayList<>();

    private static final List<String> finalResult = new ArrayList<>();
    public static void main(String[] args) {
        int round = 100;
        useNew = true;
        Map<String, BoxConfig> map = getBoxConfigMap();
        configs = new BoxConfig[]{
//                map.get("15p"),
//                map.get("20p"),
//                map.get("25p"),
//                map.get("30p"),
//                map.get("35p"),
//                map.get("40p"),
//                map.get("45p"),
//                map.get("50p"),
//                map.get("60p"),
//                map.get("70p"),
//                map.get("80p"),
                map.get("90p"),
                map.get("100p"),
                map.get("110p"),
                map.get("120p"),
                map.get("130p"),
                map.get("140p"),
//                map.get("150p")
        };

//        mainFunction(round,60);
//        mainFunction(round,70);
//        mainFunction(round,80);
//        mainFunction(round,90);
//        mainFunction(round,100);
//        mainFunction(round,110);
//        mainFunction(round,120);
//        mainFunction(round,200);
//        mainFunction(round,210);
//        mainFunction(round,220);
//        mainFunction(round,230);
//        mainFunction(round,240);
//        mainFunction(round,250);

        mainFunction(round,150);
        mainFunction(round,160);
        mainFunction(round,170);
        mainFunction(round,180);
        mainFunction(round,200);


        System.out.println("====================最终记录结果=======================");
        for (String s : finalResult) {
            System.out.println(s);
        }
    }

    private static void cleanResultList() {
        result.clear();
        remaining.clear();
        outTimeFishSize_180.clear();
        outTimeFishSize_240.clear();
        outTimeFishSize_300.clear();
        outTimeFishSize_420.clear();
        outTimeFishSize_540.clear();
        outTimeFishSize_600.clear();
        outTimeFishSize_660.clear();
        totalSize.clear();
        calSize.clear();
        subSize.clear();
    }

    private static void printResult(int round) {
        for (String s : result) {
            System.out.println(s);
        }
        //计算平均剩余
        BigDecimal sum = new BigDecimal(0);
        for (BigDecimal bigDecimal : remaining) {
            sum = sum.add(bigDecimal);
        }
        BigDecimal divide = sum.divide(BigDecimal.valueOf(remaining.size()), 2, RoundingMode.HALF_UP);
        finalResult.add(String.format("【缓存池大小：%s,暂存箱数量：%s,超时时间：%s】 测试%s次\n 测试结果：平均超时鱼：三分钟%s条,四分钟%s条,五分钟%s条,七分钟%s条,九分钟%s条,十分钟%s条,十一分钟%s条,\n平均剩余：%skg, 平均样本数：%s, 平均计算次数：%s，平均回流次数：%s",
                BUFFER_SIZE_LIMIT, CACHE_BOX_PER_SPEC,FISH_MAX_ROUND, round,
                BigDecimal.valueOf(outTimeFishSize_180.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(outTimeFishSize_240.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(outTimeFishSize_300.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(outTimeFishSize_420.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(outTimeFishSize_540.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(outTimeFishSize_600.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(outTimeFishSize_660.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                divide,
                BigDecimal.valueOf(totalSize.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(calSize.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP),
                BigDecimal.valueOf(subSize.stream().mapToInt(i -> i).average().getAsDouble()).setScale(2,RoundingMode.HALF_UP)
        ));
        cleanResultList();
    }


    public static void mainFunction(int round, int bufferSIze) {
        int size = 1;
        BUFFER_SIZE_LIMIT = bufferSIze;
        while (size <= round) {
            String uuid = UUID.randomUUID().toString();
            log.info("===============================测试{}批次开始：{}===============================", size, uuid);
            initBoxConfig();
            int fishCount = 25000;
            long start = System.currentTimeMillis();
            simulateFishFlow(fishCount);
            long end = System.currentTimeMillis();

            long timeConsuming = (end - start) / 1000L;
            log.info("===============================第{}次计算结束，耗时：{}秒===============================",
                    size, (end - start) / 1000.0);

            // 打印前20箱和后5箱
            int printLimit = Math.min(20, boxList.size());
            for (int i = 0; i < printLimit; i++) {
                Box info = boxList.get(i);
                log.info("  箱{}：规格={}，条数={}，总重={}g，鱼详情={}",
                        i, info.getSpec(), info.getFishCount(), info.getWeight(), getBoxInfo(info));
            }
            if (boxList.size() > 20) {
                log.info("  ... 中间省略 {} 箱 ...", boxList.size() - 25);
                for (int i = Math.max(20, boxList.size() - 5); i < boxList.size(); i++) {
                    Box info = boxList.get(i);
                    log.info("  箱{}：规格={}，条数={}，总重={}g，鱼详情={}",
                            i, info.getSpec(), info.getFishCount(), info.getWeight(), getBoxInfo(info));
                }
            }
            log.info("===============================各规格暂存箱余量===============================");
            int totalInBoxes = 0;
            AtomicInteger remainWeight = new AtomicInteger();
            for (int i = 0; i < configs.length; i++) {
                int totalWeight = 0;
                StringBuilder sizes = new StringBuilder();
                for (int j = 0; j < CACHE_BOX_PER_SPEC; j++) {
                    List<Fish> cacheBox = cacheBoxes[i][j];
                    remainWeight.addAndGet(cacheBox.stream().mapToInt(Fish::getWeight).sum());
                    totalInBoxes += cacheBox.size();
                    log.info("  规格[{}]：阈值={}条，暂存箱总条数={}，总重={}g",
                            configs[i].getSpec(), boxThreshold[i],  cacheBox.size(), sumWeights[i][j]);
                }
            }
            log.info("===============================缓冲池各规格详情===============================");
            for (Map.Entry<String, List<Fish>> entry : bufferMap.entrySet()) {
                StringBuilder reflowStr = new StringBuilder("缓冲池鱼：");
                for (Fish fish : entry.getValue()) {
                    reflowStr.append(fish.getPrintStr()).append(" ");
                }
                log.info("  规格{}：缓冲池{}条", entry.getKey(), entry.getValue().size());
                log.info(reflowStr.toString());
            }
            log.info("===============================回流转存鱼详情===============================");
            StringBuilder reflowStr = new StringBuilder("  回流转存鱼：");
            for (Fish fish : reflowFish) {
                reflowStr.append(fish.getPrintStr()).append(" ");
            }
            log.info(reflowStr.toString());
            log.info("===============================【统计信息】===============================");
            log.info("  总鱼数量：{}", FISH_SIZE);
            log.info("  总鱼重量：{}g/ {} kg / {}t", totalFishWeight, totalFishWeight / 1000.0, totalFishWeight / 1000000.0);
            log.info("  已装箱鱼数量：{}", BOXED_FISH_COUNT);
            BigDecimal compelateRate = BigDecimal.valueOf(BOXED_FISH_COUNT).divide(BigDecimal.valueOf(FISH_SIZE), 2, RoundingMode.HALF_UP).multiply(BigDecimal.valueOf(100));
            log.info("  装箱完成率：{}%", compelateRate);
            log.info("  装箱完成箱数：{}", boxList.size());
            log.info("  剩余暂存箱数：{}", totalInBoxes);
            log.info("  失败暂存箱数：{}", failBoxList.size());
            log.info("  回流转存数量：{}", reflowFish.size());
            totalBuffered = 0;
            JSONObject bufferJson = new JSONObject();
            bufferMap.forEach((k, v) -> {
                totalBuffered += v.size();
                remainWeight.addAndGet(v.stream().mapToInt(Fish::getWeight).sum());
                bufferJson.put(k,v.stream().map(Fish::getPrintStr).toList());
            });
            log.info("  缓冲池剩余鱼数量：{}", totalBuffered);
            log.info("  缓冲池历史最大数量：{}", MAX_BUFFER_SIZE);

            for (Fish fish : reflowFish) {
                remainWeight.addAndGet(fish.getWeight());
            }
            BigDecimal divide = BigDecimal.valueOf(remainWeight.get()).divide(new BigDecimal("1000"), 2, RoundingMode.HALF_UP);
            log.info("剩余重量：{}kg",divide);
            BigDecimal percent = BigDecimal.valueOf(remainWeight.get()).divide(new BigDecimal(totalFishWeight), 4, RoundingMode.HALF_UP);
            log.info("剩余率：{}%，成功率：{}%", percent, BigDecimal.valueOf(100).subtract(percent));
            log.info("  结束原因：{}", stopReason);
            log.info("  计算次数：{}", calculateSize);
            log.info("超时的鱼：三分钟{}条,四分钟{}条,五分钟{}条,七分钟{}条,九分钟{}条,十分钟{}条,十一分钟{}条",
                    outTimeFish_180.size(),
                    outTimeFish_240.size(),
                    outTimeFish_300.size(),
                    outTimeFish_420.size(),
                    outTimeFish_540.size(),
                    outTimeFish_600.size(),
                    outTimeFish_660.size()
            );
            outTimeFishSize_180.add(outTimeFish_180.size());
            outTimeFishSize_240.add(outTimeFish_240.size());
            outTimeFishSize_300.add(outTimeFish_300.size());
            outTimeFishSize_420.add(outTimeFish_420.size());
            outTimeFishSize_540.add(outTimeFish_540.size());
            outTimeFishSize_600.add(outTimeFish_600.size());
            outTimeFishSize_660.add(outTimeFish_660.size());
            log.info("最终记录 缓存箱：{} 完成率：{}%（数量）/ {}%（重量） 剩余：{}kg ，计算次数：{}，总样本：{}",
                    BUFFER_SIZE_LIMIT,
                    compelateRate,
                    BigDecimal.valueOf(100).subtract(percent),
                    divide,
                    calculateSize, FISH_SIZE);
            result.add(String.format("最终记录 缓存箱：%s 完成率：%s（数量）/ %s（重量） 剩余：%skg ，计算次数：%s，总样本：%s",
                    BUFFER_SIZE_LIMIT,
                    compelateRate,
                    BigDecimal.valueOf(100).subtract(percent),
                    divide,
                    calculateSize, FISH_SIZE));
            calSize.add(calculateSize);
            totalSize.add(FISH_SIZE);
            remaining.add(divide);
            subSize.add(calculateSize - FISH_SIZE);
            size++;
        }

        printResult(round);
    }



    // ==================== 模拟鱼流 ====================

    public static Fish generateRandomFish(int id) {
        // 1. 均匀随机选规格
        BoxConfig cfg = configs[ThreadLocalRandom.current().nextInt(configs.length)];
        FISH_SIZE++;
        // 2. 在 [minWeight, maxWeight] 内均匀随机生成重量
        int min = cfg.getMinFishWeight();
        int max = cfg.getMaxFishWeight();
        int weight = ThreadLocalRandom.current().nextInt(min, max + 1);  // 包含上界

        BoxConfig specConfig = getSpec(weight);
        return new Fish(id, weight, 0, specConfig.getSpec());
    }
    private static void dealwithOutTime(Fish oldFish, Fish newFish){
        if (newFish.getId() - oldFish.getId()  > 180) { outTimeFish_180.put(oldFish.getId(),oldFish); }
        if (newFish.getId() - oldFish.getId()  > 240) { outTimeFish_240.put(oldFish.getId(),oldFish); }
        if (newFish.getId() - oldFish.getId()  > 300) { outTimeFish_300.put(oldFish.getId(),oldFish); }
        if (newFish.getId() - oldFish.getId()  > 420) { outTimeFish_420.put(oldFish.getId(),oldFish); }
        if (newFish.getId() - oldFish.getId()  > 540) { outTimeFish_540.put(oldFish.getId(),oldFish); }
        if (newFish.getId() - oldFish.getId()  > 600) { outTimeFish_600.put(oldFish.getId(),oldFish); }
        if (newFish.getId() - oldFish.getId()  > 660) { outTimeFish_660.put(oldFish.getId(),oldFish); }
    }
    private static void outTimmFishMethod(Fish newFish) {
        //处理超时鱼
        bufferMap.forEach((k, v) -> {
            for (Fish fish1 : v) {
                dealwithOutTime(fish1,newFish);
            }
        });
        for (int i = 0; i < cacheBoxes.length; i++) {
            List<Fish>[] cacheBox = cacheBoxes[i];
            for (int j = 0; j < cacheBox.length; j++) {
                List<Fish> box = cacheBox[j];
                boolean flag = false;
                for (Fish fish1 : box) {
                    dealwithOutTime(fish1,newFish);
                }
            }
        }

    }

    private static void simulateFishFlow(int fishCount) {
        long lastLog = System.currentTimeMillis();
        int i = 1;
        while (totalFishWeight < limitWeight) {
//            Fish fish = generateFish(i);//随机
            Fish fish = generateRandomFish(i);
            //处理超时
            outTimmFishMethod(fish);

            totalFishWeight+=fish.getWeight();

            processNewFish(fish);
            MAX_BUFFER_SIZE = Math.max(MAX_BUFFER_SIZE, totalBuffered);
            // 每5秒或每5000条打印一次进度
            long now = System.currentTimeMillis();
            if (i > 0 && (i % 10000 == 0 || now - lastLog > 10000)) {
                double progress = 100 * ((double) totalFishWeight / (limitWeight));
                log.info("进度：{}/{} ({}%)，缓冲池：{}，已装箱：{}箱/{}条，失败箱子：{}，回流：{}",
                        totalFishWeight, "10t",
                        BigDecimal.valueOf(progress).setScale(2, RoundingMode.HALF_UP),
                        totalBuffered, boxList.size(), BOXED_FISH_COUNT,failBoxList.size(), reflowFish.size());
                lastLog = now;
            }
            if (reflowFish != null && (reflowFish.size() % 100 == 0)){
                ArrayList<Fish> arrayList = new ArrayList<>(reflowFish);
                reflowFish.clear();
                for (Fish newFish : arrayList) {
                    processNewFish(newFish);
                }
            }
            i += 1;
        }
        if (reflowFish != null && !reflowFish.isEmpty()){
            ArrayList<Fish> arrayList = new ArrayList<>(reflowFish);
            reflowFish.clear();
            for (Fish newFish : arrayList) {
                processNewFish(newFish);
            }
        }
//        finishWithBuffer();
        if (stopReason == null) stopReason = "样本处理完成";
    }

    // ==================== 核心处理 ====================

    /**
     * 处理新鱼流程：
     * ① 优先尝试直接装箱（新鱼 + 暂存箱 + 缓冲池FIFO鱼 → 完成一箱）
     * ② 装箱失败，检查暂存箱是否还有空间（未达阈值），有则放入暂存箱
     * ③ 暂存箱都已满（达到阈值），则放入缓冲池
     */
    private static void processNewFish(Fish fish) {
        calculateSize += 1; //计算次数
        int specIdx = getConfigIndex(fish.getSpec());
        if (specIdx < 0) return;

        // ===== ① 优先尝试装箱 =====
        // 新鱼直接参与装箱匹配：遍历同规格的暂存箱，尝试用暂存箱+新鱼+缓冲池鱼凑一箱
        boolean boxed = tryDirectPacking(specIdx, fish);
        if (boxed) {
            return; // 装箱成功，新鱼已被消费
        }

        // ===== ② 尝试放入暂存箱（未达阈值的暂存箱）=====
        boolean placedInCache = tryPlaceInCacheBox(specIdx, fish);
        if (placedInCache) {
            // 放入暂存箱后，检查该暂存箱是否可以装箱
            checkAndPackSpec(specIdx);
            return;
        }

        // ===== ③ 所有暂存箱都已满，放入缓冲池 =====
        // 缓冲池满的处理
        if (totalBuffered >= BUFFER_SIZE_LIMIT) {
            addToBuffer(fish);
            boolean matched = tryAllSpecsPacking();
            if (!matched) {
                // 匹配失败：取出新鱼 + 挤走最老鱼
                removeFromBuffer(fish);
                Fish evicted = evictOldest();
                if (evicted != null){
                    reflowFish.add(evicted);
                }
                addToBuffer(fish);
            }
        } else {
            addToBuffer(fish);
            // 触发匹配尝试
            tryMatchForSpec(fish.getSpec());
        }
    }

    /**
     * 尝试直接用新鱼完成装箱
     * 遍历同规格的所有暂存箱，尝试：暂存箱鱼 + 新鱼 + 缓冲池FIFO鱼 = 一箱
     */
    private static boolean tryDirectPacking(int specIdx, Fish fish) {
        BoxConfig cfg = configs[specIdx];
        String mainSpec = cfg.getSpec();
        List<String> specList = cfg.getSpecList();

        // 遍历该规格的4个暂存箱
        for (int bj = 0; bj < CACHE_BOX_PER_SPEC; bj++) {
            List<Fish> cacheBox = cacheBoxes[specIdx][bj];
            if (cacheBox.isEmpty()) continue; // 空暂存箱无法参与装箱

            int curCnt = cacheBox.size() + 1; // 暂存箱鱼 + 新鱼
            int curW = sumWeights[specIdx][bj] + fish.getWeight();

            // 条数或重量超限
            if (curCnt > cfg.getMaxFishCount()) continue;
            if (curW > MAX_WEIGHT) continue;

            // 情况A：暂存箱+新鱼 已经满足装箱条件
            if (curCnt >= cfg.getMinFishCount() && curW >= MIN_WEIGHT && curW <= MAX_WEIGHT) {
                // 把新鱼加入暂存箱，直接装箱
                cacheBox.add(fish);
                sumWeights[specIdx][bj] += fish.getWeight();
                completeBox(specIdx, bj, new ArrayList<>());
                return true;
            }

            // 情况B：需要从缓冲池补鱼
            int needLow = MIN_WEIGHT - curW;
            int needHigh = MAX_WEIGHT - curW;
            int minAdd = Math.max(1, cfg.getMinFishCount() - curCnt);
            int maxAdd = cfg.getMaxFishCount() - curCnt;

            if (needHigh < 0 || maxAdd <= 0) continue;

            // B1：纯同规格缓冲池鱼
            List<Fish> mainBuf = bufferMap.getOrDefault(mainSpec, new ArrayList<>());
            if (!mainBuf.isEmpty()) {
                FreqTable mainTable = new FreqTable(mainBuf);
                for (int k = minAdd; k <= maxAdd; k++) {
                    List<Fish> res = mainTable.findCombination(k, Math.max(0, needLow), needHigh);
                    if (res != null) {
                        // 装箱：暂存箱 + 新鱼 + 缓冲池鱼
                        cacheBox.add(fish);
                        sumWeights[specIdx][bj] += fish.getWeight();
                        completeBox(specIdx, bj, res);
                        return true;
                    }
                }
            }

            // B2：1条相邻规格 + 其余同规格缓冲池鱼
            for (String adjSpec : specList) {
                if (adjSpec.equals(mainSpec)) continue;
                List<Fish> adjBuf = bufferMap.getOrDefault(adjSpec, new ArrayList<>());
                if (adjBuf.isEmpty()) continue;
                for (Fish adj : adjBuf) {
                    int remLow = needLow - adj.getWeight();
                    int remHigh = needHigh - adj.getWeight();
                    int needMin2 = Math.max(0, minAdd - 1);
                    int needMax2 = maxAdd - 1;
                    if (needMin2 <= needMax2 && !mainBuf.isEmpty()) {
                        FreqTable reduced = new FreqTable(mainBuf);
                        reduced.remove(adj);
                        for (int k = Math.max(0, needMin2); k <= needMax2; k++) {
                            List<Fish> res = reduced.findCombination(k, Math.max(0, remLow), remHigh);
                            if (res != null) {
                                res.add(0, adj); // 相邻规格放前面（先入先出语义）
                                cacheBox.add(fish);
                                sumWeights[specIdx][bj] += fish.getWeight();
                                completeBox(specIdx, bj, res);
                                return true;
                            }
                        }
                    }
                }
            }
        }
        return false;
    }

    /**
     * 尝试将新鱼放入该规格的某个暂存箱（未达阈值的）
     */
    private static boolean tryPlaceInCacheBox(int specIdx, Fish fish) {
        BoxConfig cfg = configs[specIdx];

        int minNextW = minPossibleNextWeight[specIdx];
        // 优先放入已有鱼且不超重的暂存箱
        for (int j = 0; j < CACHE_BOX_PER_SPEC; j++) {
            List<Fish> box = cacheBoxes[specIdx][j];
            if (box.isEmpty()) continue;
            // 检查是否已达阈值（达到阈值不再接收新鱼）
            if (box.size() >= boxThreshold[specIdx]) continue;
            // 检查是否超重或超条数
            int newCount = box.size() + 1;
            int newWeight = sumWeights[specIdx][j] + fish.getWeight();
            if (newCount >= cfg.getMaxFishCount()) continue;
            if (newWeight > MAX_WEIGHT) continue;

            // 死区
            if (isInErrorInterval(specIdx, newWeight)) return false;

            boolean isComplete = (newWeight >= MIN_WEIGHT && newCount >= cfg.getMinFishCount());
            if (!isComplete) {
                int remaining = MAX_WEIGHT - newWeight;
                if (remaining < minNextW) {
                    return false;
                }

                // 在 tryPutInSpecBox 中，放入前：
//            if (leadsToDangerousState(specIdx, cacheBoxes[specIdx][boxIdx].size(),
//                    sumWeights[specIdx][boxIdx], fish.getWeight())) {
////                log.info("危险禁止");
//                return false;  // 拒绝，避免进入危险状态
//            }

            }

            box.add(fish);
            sumWeights[specIdx][j] += fish.getWeight();
            return true;
        }

        // 放入空暂存箱（空箱没有阈值限制）
        for (int j = 0; j < CACHE_BOX_PER_SPEC; j++) {
            if (cacheBoxes[specIdx][j].isEmpty() && boxThreshold[specIdx] > 0) {
                cacheBoxes[specIdx][j].add(fish);
                sumWeights[specIdx][j] += fish.getWeight();
                return true;
            }
        }

        return false; // 所有暂存箱都已满（达到阈值或超重/超条数）
    }

    /**
     * 检查指定规格的所有暂存箱是否可以装箱
     */
    private static void checkAndPackSpec(int specIdx) {
        boolean packed;
        int loops = 0;
        do {
            packed = false;
            for (int j = 0; j < CACHE_BOX_PER_SPEC; j++) {
                List<Fish> cacheBox = cacheBoxes[specIdx][j];
                // 暂存箱达到阈值或至少有最小装箱条数，尝试装箱
                if (cacheBox.size() >= boxThreshold[specIdx] || cacheBox.size() >= configs[specIdx].getMinFishCount()) {
                    if (tryPackFromCache(specIdx, j)) {
                        packed = true;
                        break;
                    }
                }

                if (cacheBox.size() == boxThreshold[specIdx] && sumWeights[specIdx][j] > 0){
                    BoxConfig cfg = configs[specIdx];
                    List<String> specList = cfg.getSpecList();
                    int weight = sumWeights[specIdx][j];
                    int needMinFish = MIN_WEIGHT-weight;
                    int needMaxFish = MAX_WEIGHT-weight;
                    int specMinFish = cfg.getMinFishWeight();
                    int specMaxFish = cfg.getMaxFishWeight();
                    for (String spec : specList) {
                        int configIndex = getConfigIndex(spec);
                        if (configIndex > 0) {
                            BoxConfig config = configs[configIndex];
                            specMinFish = Math.min(specMinFish, config.getMinFishWeight());
                            specMaxFish = Math.max(specMaxFish, config.getMaxFishWeight());
                        }

                    };
                    boolean flag = false;
                    //如果比剩余克数大于最大鱼，则需要大于1条规格最小鱼和本规格鱼之和
                    if (specMinFish > needMaxFish){
                        //则
                    }
                    if (needMinFish < specMinFish && needMaxFish < specMinFish){
                        //此情况没有满足装箱的最小鱼产生，直接剔除，清空
                        List<Fish> boxFishes = new ArrayList<>(cacheBoxes[specIdx][j]);
                        int totalWeight = sumWeights[specIdx][j];
                        failBoxList.add(new Box(configs[specIdx].getSpec(), boxFishes, totalWeight, boxFishes.size()));
                        // 清空暂存箱
                        cacheBoxes[specIdx][j] = new ArrayList<>();
                        sumWeights[specIdx][j] = 0;
                    }
                }
            }
            loops++;
        } while (packed && loops < 20);
    }

    /**
     * 尝试从暂存箱+缓冲池组合完成装箱（不包含新鱼，用于定期检查）
     */
    private static boolean tryPackFromCache(int specIdx, int boxIdx) {
        List<Fish> cacheBox = cacheBoxes[specIdx][boxIdx];
        int curCnt = cacheBox.size();
        int curW = sumWeights[specIdx][boxIdx];
        BoxConfig cfg = configs[specIdx];
        String mainSpec = cfg.getSpec();
        List<String> specList = cfg.getSpecList();

        // 条数不够
        if (curCnt < cfg.getMinFishCount() && curCnt < boxThreshold[specIdx]) return false;
        // 已超重
        if (curW > MAX_WEIGHT) return false;

        // 情况A：暂存箱自身满足条件
        if (curCnt >= cfg.getMinFishCount() && curW >= MIN_WEIGHT && curW <= MAX_WEIGHT && curCnt <= cfg.getMaxFishCount()) {
            completeBox(specIdx, boxIdx, new ArrayList<>());
            return true;
        }

        // 情况B：需要缓冲池补鱼
        int needLow = MIN_WEIGHT - curW;
        int needHigh = MAX_WEIGHT - curW;
        int minAdd = Math.max(1, cfg.getMinFishCount() - curCnt);
        int maxAdd = cfg.getMaxFishCount() - curCnt;

        if (needHigh < 0 || maxAdd <= 0) return false;

        // B1：纯同规格
        List<Fish> mainBuf = bufferMap.getOrDefault(mainSpec, new ArrayList<>());
        if (!mainBuf.isEmpty()) {
            FreqTable mainTable = new FreqTable(mainBuf);
            for (int k = minAdd; k <= maxAdd; k++) {
                List<Fish> res = mainTable.findCombination(k, Math.max(0, needLow), needHigh);
                if (res != null) {
                    completeBox(specIdx, boxIdx, res);
                    return true;
                }
            }
        }

        // B2：1条相邻 + 其余同规格
        for (String adjSpec : specList) {
            if (adjSpec.equals(mainSpec)) continue;
            List<Fish> adjBuf = bufferMap.getOrDefault(adjSpec, new ArrayList<>());
            if (adjBuf.isEmpty()) continue;
            for (Fish adj : adjBuf) {
                int remLow = needLow - adj.getWeight();
                int remHigh = needHigh - adj.getWeight();
                int needMin2 = Math.max(0, minAdd - 1);
                int needMax2 = maxAdd - 1;
                if (needMin2 <= needMax2 && !mainBuf.isEmpty()) {
                    FreqTable reduced = new FreqTable(mainBuf);
                    reduced.remove(adj);
                    for (int k = Math.max(0, needMin2); k <= needMax2; k++) {
                        List<Fish> res = reduced.findCombination(k, Math.max(0, remLow), remHigh);
                        if (res != null) {
                            res.add(0, adj);
                            completeBox(specIdx, boxIdx, res);
                            return true;
                        }
                    }
                }
            }
        }
        return false;
    }

    /**
     * 尝试所有规格所有暂存箱的装箱匹配
     */
    private static boolean tryAllSpecsPacking() {
        for (int si = 0; si < configs.length; si++) {
            for (int bj = 0; bj < CACHE_BOX_PER_SPEC; bj++) {
                if (!cacheBoxes[si][bj].isEmpty() &&
                        cacheBoxes[si][bj].size() >= boxThreshold[si]) {
                    if (tryPackFromCache(si, bj)) return true;
                }
            }
        }
        // 也尝试空暂存箱从缓冲池直接装箱
        return tryEmptyBoxPacking();
    }

    /**
     * 触发指定规格相关的装箱匹配
     */
    private static void tryMatchForSpec(String spec) {
        Set<Integer> affectedSpecs = new LinkedHashSet<>();
        for (int i = 0; i < configs.length; i++) {
            if (configs[i].getSpecList().contains(spec)) {
                affectedSpecs.add(i);
            }
        }
        if (affectedSpecs.isEmpty()) return;

        int loops = 0, maxLoops = 10;
        boolean matched;
        do {
            matched = false;
            for (int si : affectedSpecs) {
                for (int bj = 0; bj < CACHE_BOX_PER_SPEC; bj++) {
                    List<Fish> cb = cacheBoxes[si][bj];
                    if (cb.isEmpty()) continue;
                    if (cb.size() >= boxThreshold[si] || cb.size() >= configs[si].getMinFishCount()) {
                        long now = System.currentTimeMillis();
                        int h = computeBufferHash();
                        int cacheIdx = si * CACHE_BOX_PER_SPEC + bj;
                        if (cacheIdx < lastNoMatchTime.length &&
                                lastNoMatchTime[cacheIdx] > 0 &&
                                now - lastNoMatchTime[cacheIdx] < 50 &&
                                lastNoMatchHash[cacheIdx] == h) {
                            continue;
                        }
                        if (tryPackFromCache(si, bj)) {
                            if (cacheIdx < lastNoMatchTime.length) {
                                lastNoMatchTime[cacheIdx] = 0;
                            }
                            matched = true;
                            affectedSpecs.clear();
                            for (int i = 0; i < configs.length; i++) {
                                affectedSpecs.add(i);
                            }
                            break;
                        } else {
                            if (cacheIdx < lastNoMatchTime.length) {
                                lastNoMatchTime[cacheIdx] = now;
                                lastNoMatchHash[cacheIdx] = h;
                            }
                        }
                    }
                }
                if (matched) break;
            }
            loops++;
        } while (matched && loops < maxLoops);
    }

    /**
     * 尝试从空暂存箱+缓冲池直接装箱
     */
    private static boolean tryEmptyBoxPacking() {
        for (int si = 0; si < configs.length; si++) {
            String mainSpec = configs[si].getSpec();
            int minK = configs[si].getMinFishCount();
            int maxK = configs[si].getMaxFishCount();

            List<Fish> mainBuf = bufferMap.getOrDefault(mainSpec, new ArrayList<>());
            if (!mainBuf.isEmpty()) {
                FreqTable mainTable = new FreqTable(mainBuf);
                for (int k = minK; k <= maxK; k++) {
                    List<Fish> res = mainTable.findCombination(k, MIN_WEIGHT, MAX_WEIGHT);
                    if (res != null) {
                        for (Fish f : res) removeFromBuffer(f);
                        boxList.add(new Box(mainSpec, new ArrayList<>(res),
                                res.stream().mapToInt(Fish::getWeight).sum(), res.size()));
                        BOXED_FISH_COUNT += res.size();
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private static int computeBufferHash() {
        int h = 0;
        for (Map.Entry<String, List<Fish>> e : bufferMap.entrySet()) {
            h = 31 * h + e.getKey().hashCode();
            h = 31 * h + e.getValue().size();
        }
        return h;
    }

    // ==================== 重量频率表（核心数据结构）====================

    /**
     * 将鱼按重量分组，用于高效组合枚举
     * 每个重量组内的鱼保持 FIFO 顺序
     */
    private static class FreqTable {
        final List<Integer> weights = new ArrayList<>();      // 去重重量，升序
        final List<Integer> counts = new ArrayList<>();       // 每个重量的条数
        final List<List<Fish>> fishByWeight = new ArrayList<>(); // 每个重量对应的鱼列表（FIFO）

        FreqTable(List<Fish> fishList) {
            Map<Integer, List<Fish>> map = new LinkedHashMap<>();
            for (Fish f : fishList) {
                map.computeIfAbsent(f.getWeight(), k -> new ArrayList<>()).add(f);
            }
            List<Integer> sortedWeights = new ArrayList<>(map.keySet());
            Collections.sort(sortedWeights);
            for (int w : sortedWeights) {
                weights.add(w);
                List<Fish> list = map.get(w);
                counts.add(list.size());
                fishByWeight.add(list);
            }
        }

        FreqTable(FreqTable other) {
            this.weights.addAll(other.weights);
            this.counts.addAll(other.counts);
            for (List<Fish> list : other.fishByWeight) {
                this.fishByWeight.add(new ArrayList<>(list));
            }
        }

        FreqTable copy() {
            return new FreqTable(this);
        }

        void remove(Fish f) {
            for (int i = 0; i < fishByWeight.size(); i++) {
                List<Fish> list = fishByWeight.get(i);
                if (list.remove(f)) {
                    counts.set(i, counts.get(i) - 1);
                    if (counts.get(i) == 0) {
                        weights.remove(i);
                        counts.remove(i);
                        fishByWeight.remove(i);
                    }
                    return;
                }
            }
        }

        /**
         * 选恰好k条鱼，总重在[low, high]内
         * DFS枚举重量组合，取每个重量的前take条（保证FIFO）
         */
        List<Fish> findCombination(int k, int low, int high) {
            if (k <= 0) return null;
            if (low < 0) low = 0;
            if (minSum(k) > high || maxSum(k) < low) return null;
            List<Fish> result = new ArrayList<>();
            boolean found = dfs(0, k, low, high, 0, result);
            return found ? result : null;
        }

        private long minSum(int k) {
            long s = 0;
            int need = k;
            for (int i = 0; i < weights.size() && need > 0; i++) {
                int take = Math.min(need, counts.get(i));
                s += (long) weights.get(i) * take;
                need -= take;
            }
            return s;
        }

        private long maxSum(int k) {
            long s = 0;
            int need = k;
            for (int i = weights.size() - 1; i >= 0 && need > 0; i--) {
                int take = Math.min(need, counts.get(i));
                s += (long) weights.get(i) * take;
                need -= take;
            }
            return s;
        }

        private boolean dfs(int idx, int remain, int low, int high, int curSum, List<Fish> out) {
            if (remain == 0) {
                return curSum >= low;
            }
            if (idx >= weights.size()) return false;

            int w = weights.get(idx);
            int maxTake = Math.min(remain, counts.get(idx));

            // 剪枝：最优情况也不在范围内
            long bestMin = curSum + (long) w * maxTake;
            if (remain > maxTake) {
                int needMore = remain - maxTake;
                for (int j = idx + 1; j < weights.size() && needMore > 0; j++) {
                    int t = Math.min(needMore, counts.get(j));
                    bestMin += (long) weights.get(j) * t;
                    needMore -= t;
                }
            }
            if (bestMin > high) return false;

            long bestMax = curSum + (long) w * maxTake;
            if (remain > maxTake) {
                int needMore = remain - maxTake;
                for (int j = weights.size() - 1; j > idx && needMore > 0; j--) {
                    int t = Math.min(needMore, counts.get(j));
                    bestMax += (long) weights.get(j) * t;
                    needMore -= t;
                }
            }
            if (bestMax < low) return false;

            // 尝试取0~maxTake条当前重量的鱼（优先取前面的，保证FIFO）
            for (int take = 0; take <= maxTake; take++) {
                int newSum = curSum + w * take;
                if (newSum > high) break;

                List<Fish> chosen = fishByWeight.get(idx);
                for (int t = 0; t < take; t++) out.add(chosen.get(t));

                if (dfs(idx + 1, remain - take, low, high, newSum, out)) return true;

                for (int t = 0; t < take; t++) out.remove(out.size() - 1);
            }
            return false;
        }
    }

    // ==================== 装箱操作 ====================

    /**
     * 完成装箱
     * @param specIdx  规格索引
     * @param boxIdx   暂存箱索引
     * @param usedFish 从缓冲池取出的鱼（FIFO顺序）
     */
    private static void completeBox(int specIdx, int boxIdx, List<Fish> usedFish) {
        List<Fish> boxFishes = new ArrayList<>(cacheBoxes[specIdx][boxIdx]);
        boxFishes.addAll(usedFish);
        int totalWeight = sumWeights[specIdx][boxIdx] +
                usedFish.stream().mapToInt(Fish::getWeight).sum();
        Box box = new Box(configs[specIdx].getSpec(), boxFishes, totalWeight, boxFishes.size());
        boxList.add(box);
        BOXED_FISH_COUNT += boxFishes.size();

        // 从缓冲池移除已使用的鱼
        for (Fish f : usedFish) {
            removeFromBuffer(f);
        }

        // 清空暂存箱
        cacheBoxes[specIdx][boxIdx] = new ArrayList<>();
        sumWeights[specIdx][boxIdx] = 0;

        // 从缓冲池补充该暂存箱（优先同规格FIFO）
        fillBoxFromBuffer(specIdx, boxIdx);
    }

    /**
     * 从缓冲池补充暂存箱（优先同规格，遵循FIFO）
     */
    private static void fillBoxFromBuffer(int specIdx, int boxIdx) {
        String spec = configs[specIdx].getSpec();
        List<Fish> specBuf = bufferMap.get(spec);
        if (specBuf == null || specBuf.isEmpty()) {
            return;
        }
        if (0 == boxThreshold[specIdx]){
            return;
        }

        // 按ID升序排序，让最老的鱼（ID最小）排在前面
        specBuf.sort(Comparator.comparingInt(Fish::getId));

        Iterator<Fish> it = specBuf.iterator();
        while (cacheBoxes[specIdx][boxIdx].size() < boxThreshold[specIdx] && it.hasNext()) {
            Fish f = it.next();
            int newWeight = sumWeights[specIdx][boxIdx] + f.getWeight();
            if (newWeight <= MAX_WEIGHT) {
                it.remove();               // 从缓冲池中移除
                totalBuffered--;           // 更新缓冲池计数
                cacheBoxes[specIdx][boxIdx].add(f);    // 加入暂存箱
                sumWeights[specIdx][boxIdx] = newWeight; // 更新暂存箱重量
            }
        }

        // 如果该规格缓冲池已空，从map中删除
        if (specBuf.isEmpty()) {
            bufferMap.remove(spec);
        }
    }

    private static void addToBuffer(Fish fish) {
        bufferMap.computeIfAbsent(fish.getSpec(), k -> new ArrayList<>()).add(fish);
        totalBuffered++;
    }

    private static void removeFromBuffer(Fish f) {
        List<Fish> list = bufferMap.get(f.getSpec());
        if (list != null && list.remove(f)) {
            totalBuffered--;
            if (list.isEmpty()) bufferMap.remove(f.getSpec());
        }
    }

    /**
     * 挤出缓冲池中最老的鱼（全局FIFO）
     */
    private static Fish evictOldest() {
        Fish oldest = null;
        String oldestSpec = null;
        for (Map.Entry<String, List<Fish>> e : bufferMap.entrySet()) {
            if (!e.getValue().isEmpty()) {
                Fish first = e.getValue().get(0);
                if (oldest == null || first.getId() < oldest.getId()) {
                    oldest = first;
                    oldestSpec = e.getKey();
                }
            }
        }
        if (oldest != null) {
            List<Fish> list = bufferMap.get(oldestSpec);
            list.remove(oldest);
            totalBuffered--;
            if (list.isEmpty()) bufferMap.remove(oldestSpec);
        }
        return oldest;
    }

    // ==================== 收尾处理 ====================

    private static void finishWithBuffer() {
        int loops = 0, maxLoops = 500;
        while (totalBuffered > 0 && loops < maxLoops) {
            loops++;
            boolean progress = false;

            // 从缓冲池补充各暂存箱
            for (int si = 0; si < configs.length; si++) {
                for (int bj = 0; bj < CACHE_BOX_PER_SPEC; bj++) {
                    int before = totalBuffered;
                    fillBoxFromBuffer(si, bj);
                    if (totalBuffered < before) progress = true;
                }
            }

            // 尝试装箱
            for (int si = 0; si < configs.length; si++) {
                for (int bj = 0; bj < CACHE_BOX_PER_SPEC; bj++) {
                    if (!cacheBoxes[si][bj].isEmpty() &&
                            cacheBoxes[si][bj].size() >= Math.min(boxThreshold[si], configs[si].getMinFishCount())) {
                        if (tryPackFromCache(si, bj)) {
                            progress = true;
                            break;
                        }
                    }
                }
                if (progress) break;
            }

            if (!progress && !tryRelaxedComplete()) break;
        }
        if (totalBuffered > 0) stopReason = "缓冲池剩余" + totalBuffered + "条无法装箱";
    }

    /**
     * 宽松收尾：尝试所有可能的组合
     */
    private static boolean tryRelaxedComplete() {
        // 1. 处理半成品暂存箱
        for (int si = 0; si < configs.length; si++) {
            for (int bj = 0; bj < CACHE_BOX_PER_SPEC; bj++) {
                if (cacheBoxes[si][bj].isEmpty()) continue;
                int curW = sumWeights[si][bj];
                int curCnt = cacheBoxes[si][bj].size();
                String mainSpec = configs[si].getSpec();
                int minK = configs[si].getMinFishCount();
                int maxK = configs[si].getMaxFishCount();

                int needLow = MIN_WEIGHT - curW;
                int needHigh = MAX_WEIGHT - curW;
                int minAdd = Math.max(0, minK - curCnt);
                int maxAdd = maxK - curCnt;

                List<Fish> mainBuf = bufferMap.getOrDefault(mainSpec, new ArrayList<>());

                // 纯同规格
                if (!mainBuf.isEmpty() && minAdd <= maxAdd) {
                    FreqTable mt = new FreqTable(mainBuf);
                    int nMin = Math.max(0, minAdd);
                    for (int k = nMin; k <= maxAdd; k++) {
                        List<Fish> res = mt.findCombination(k, Math.max(0, needLow), needHigh);
                        if (res != null) { completeBox(si, bj, res); return true; }
                    }
                }

                // 1条相邻
                for (String adjSpec : configs[si].getSpecList()) {
                    if (adjSpec.equals(mainSpec)) continue;
                    List<Fish> adjBuf = bufferMap.getOrDefault(adjSpec, new ArrayList<>());
                    for (Fish adj : adjBuf) {
                        int remLow = needLow - adj.getWeight();
                        int remHigh = needHigh - adj.getWeight();
                        int nMin2 = Math.max(0, minAdd - 1);
                        int nMax2 = maxAdd - 1;
                        if (nMin2 <= nMax2 && !mainBuf.isEmpty()) {
                            FreqTable reduced = new FreqTable(mainBuf);
                            reduced.remove(adj);
                            for (int k = Math.max(0, nMin2); k <= nMax2; k++) {
                                List<Fish> res = reduced.findCombination(k, Math.max(0, remLow), remHigh);
                                if (res != null) {
                                    res.add(0, adj);
                                    completeBox(si, bj, res);
                                    return true;
                                }
                            }
                        }
                    }
                }
            }
        }

        // 2. 从缓冲池直接装箱
        return tryEmptyBoxPacking();
    }

    // ==================== 工具方法 ====================

    private static int getConfigIndex(String spec) {
        for (int i = 0; i < configs.length; i++)
            if (configs[i].getSpec().equals(spec)) return i;
        return -1;
    }

    private static BoxConfig getSpec(int weight) {
        for (BoxConfig c : configs)
            if (weight >= c.getMinFishWeight() && weight <= c.getMaxFishWeight()) return c;
        return null;
    }

    @SuppressWarnings("unchecked")
    private static void initBoxConfig() {
        stopReason = null;
        FISH_SIZE = 0;
        BOXED_FISH_COUNT = 0;
        MAX_BUFFER_SIZE = 0;
        totalBuffered = 0;
        bufferMap.clear();
        boxList.clear();
        outTimeFish_180.clear();
        outTimeFish_240.clear();
        outTimeFish_300.clear();
        outTimeFish_420.clear();
        outTimeFish_540.clear();
        outTimeFish_600.clear();
        outTimeFish_660.clear();
//        reflowCount = 0;
        reflowFish.clear();
        totalFishWeight = 0;
        calculateSize = 0;

        errorInterval = new HashMap<>();


        int specCount = configs.length;
        cacheBoxes = new List[specCount][CACHE_BOX_PER_SPEC];
        sumWeights = new int[specCount][CACHE_BOX_PER_SPEC];
        boxThreshold = new int[specCount];
        lastNoMatchTime = new long[specCount * CACHE_BOX_PER_SPEC];
        lastNoMatchHash = new int[specCount * CACHE_BOX_PER_SPEC];

        minPossibleNextWeight = new int[specCount];
        for (int i = 0; i < specCount; i++) {
            BoxConfig config = configs[i];
            for (int j = 0; j < CACHE_BOX_PER_SPEC; j++) {
                cacheBoxes[i][j] = new ArrayList<>();
                sumWeights[i][j] = 0;
            }
            // 阈值 = 最小装箱条数，达到后暂存箱不再接收新鱼
            if (BOX_TYPE == 1){
                boxThreshold[i] = (int) (config.getMinFishCount()-1);
            }else if (BOX_TYPE == 2){
//                boxThreshold[i] = (int) ((config.getMinFishCount())*BOX_RATE);
                boxThreshold[i] = config.getPreload();
            }else {
                boxThreshold[i] = 0;
            }
//
//            boxThreshold[i] = (int) (configs[i].getMinFishCount()-1);

            int minW = config.getMinFishWeight();
            for (String spec : config.getSpecList()) {
                if (spec.equals(config.getSpec())) continue;
                int idx = getConfigIndex(spec);
                if (idx >= 0) {
                    minW = Math.min(minW, configs[idx].getMinFishWeight());
                }
            }
            minPossibleNextWeight[i] = minW;
            errorInterval.put(i, getErrorInterval(i));
        }
        for (int i = 0; i < specCount * CACHE_BOX_PER_SPEC; i++) {
            lastNoMatchTime[i] = 0;
            lastNoMatchHash[i] = 0;
        }

        FISH_MIN_WEIGHT = configs[specCount - 1].getMinFishWeight(); // 45p的min
        FISH_MAX_WEIGHT = configs[0].getMaxFishWeight();             // 20p的max
    }



    private static List<int[]> getErrorInterval(int specIndex) {
        BoxConfig config = configs[specIndex];
        int minFishWeight = config.getMinFishWeight();
        int maxFishWeight = config.getMaxFishWeight();
        List<String> specList = config.getSpecList();
        int maxWeight = MAX_WEIGHT;
        int minWeight = MIN_WEIGHT;
        boolean first = true;
        int neighborMin = 0;
        int neighborMax = 0;

        for (String spec : specList) {
            int idx = getConfigIndex(spec);
            if (idx >= 0) {
                BoxConfig cfg = configs[idx];
                if (neighborMin == 0) {
                    neighborMin = cfg.getMinFishWeight();
                } else {
                    neighborMin = Math.min(neighborMin, cfg.getMinFishWeight());
                }
                if (neighborMax == 0) {
                    neighborMax = cfg.getMaxFishWeight();
                } else {
                    neighborMax = Math.max(neighborMax, cfg.getMaxFishWeight());
                }
            }
        }

        List<int[]> errorList = new ArrayList<>();
        while (true) {
            int currentMin = minWeight;
            int currentMax = maxWeight;
            if (first) {
                currentMax = currentMax - neighborMin;
                currentMin = currentMin - neighborMax;
                first = false;
            } else {
                currentMax = currentMax - minFishWeight;
                currentMin = currentMin - maxFishWeight;
                if (currentMax < minWeight) {
                    errorList.add(new int[]{currentMax, minWeight});
                } else {
                    break;
                }
            }
            maxWeight = currentMax;
            minWeight = currentMin;
        }
        return errorList;
    }

    private static String getBoxInfo(Box box) {
        StringBuilder sb = new StringBuilder("[");
        for (Fish f : box.getFishList()) {
            sb.append(f.getId()).append("=").append(f.getWeight()).append("g/").append(f.getSpec()).append(",");
        }
        if (sb.length() > 1) sb.setLength(sb.length() - 1);
        sb.append("]");
        return sb.toString();
    }

    private static boolean isInErrorInterval(int specIdx, int weight) {
        List<int[]> intervals = errorInterval.get(specIdx);
        if (intervals == null) return false;
        for (int[] range : intervals) {
            if (weight >= range[0] && weight <= range[1]) {
                return true;
            }
        }
        return false;
    }

    private static Map<String, BoxConfig> getBoxConfigMap() {
        Map<String, BoxConfig> map = new HashMap<>();

        if (useNew){
            //新规格
            map.put("15p", new BoxConfig("15p", 2, 7, 9, 556, 680, Arrays.asList("15p", "20p")));
            map.put("20p", new BoxConfig("20p", 2, 10, 11, 444, 555, Arrays.asList("15p", "20p", "25p")));
            map.put("25p", new BoxConfig("25p", 3, 12, 14, 358, 444, Arrays.asList("20p", "25p", "30p")));
            map.put("30p", new BoxConfig("30p", 3, 15, 16, 308, 358, Arrays.asList("25p", "30p", "35p")));
            map.put("35p", new BoxConfig("35p", 4, 17, 19, 265, 307, Arrays.asList("30p", "35p", "40p")));
            map.put("40p", new BoxConfig("40p", 4, 20, 21, 235, 264, Arrays.asList("35p", "40p", "45p")));
            map.put("45p", new BoxConfig("45p", 5, 22, 23, 215, 235, Arrays.asList("40p", "45p", "50p")));
            map.put("50p", new BoxConfig("50p", 8, 25, 26, 182, 214, Arrays.asList("45p", "50p", "60p")));
            map.put("60p", new BoxConfig("60p", 13,29, 31, 155, 181, Arrays.asList("50p", "60p", "70p")));
            map.put("70p", new BoxConfig("70p", 14,34, 36, 135, 154, Arrays.asList("60p", "70p", "80p")));
            map.put("80p", new BoxConfig("80p", 16,39, 41, 118, 134, Arrays.asList("70p", "80p", "90p")));
            map.put("90p", new BoxConfig("90p", 18,44, 46, 105, 117, Arrays.asList("80p", "90p", "100p")));
            map.put("100p", new BoxConfig("100p", 25,49, 51, 95, 104, Arrays.asList("90p", "100p", "110p")));
            map.put("110p", new BoxConfig("110p", 28,54, 56, 87, 94, Arrays.asList("100p", "110p", "120p")));
            map.put("120p", new BoxConfig("120p", 30,59, 61, 80, 86, Arrays.asList("110p", "120p", "130p")));
            map.put("130p", new BoxConfig("130p", 33,64, 66, 74, 79, Arrays.asList("120p", "130p", "140p")));
            map.put("140p", new BoxConfig("140p", 35,69, 71, 69, 73, Arrays.asList("130p", "140p", "150p")));
            map.put("150p", new BoxConfig("150p", 37,74, 76, 64, 68, Arrays.asList("140p", "150p")));
        }else {
            //旧规格
            map.put("15p", new BoxConfig("15p", 2, 8, 9, 566, 700, Arrays.asList("15p", "20p")));
            map.put("20p", new BoxConfig("20p", 2, 10, 11, 446, 565, Arrays.asList("15p", "20p", "25p")));
            map.put("25p", new BoxConfig("25p", 3, 12, 14, 366, 445, Arrays.asList("20p", "25p", "30p")));
            map.put("30p", new BoxConfig("30p", 3, 15, 16, 306, 365, Arrays.asList("25p", "30p", "35p")));
            map.put("35p", new BoxConfig("35p", 4, 17, 19, 266, 305, Arrays.asList("30p", "35p", "40p")));
            map.put("40p", new BoxConfig("40p", 4, 20, 21, 231, 265, Arrays.asList("35p", "40p", "45p")));
            map.put("45p", new BoxConfig("45p", 5, 22, 23, 211, 230, Arrays.asList("40p", "45p", "50p")));
            map.put("50p", new BoxConfig("50p", 8, 25, 26, 183, 210, Arrays.asList("45p", "50p", "60p")));
            map.put("60p", new BoxConfig("60p", 15, 30, 31, 153, 182, Arrays.asList("50p", "60p", "70p")));
            map.put("70p", new BoxConfig("70p", 18, 35, 36, 133, 152, Arrays.asList("60p", "70p", "80p")));
            map.put("80p", new BoxConfig("80p", 20, 40, 41, 116, 132, Arrays.asList("70p", "80p", "90p")));
            map.put("90p", new BoxConfig("90p", 23, 45, 46, 106, 115, Arrays.asList("80p", "90p", "100p")));
            map.put("100p", new BoxConfig("100p", 25, 50, 51, 96, 105, Arrays.asList("90p", "100p", "110p")));
            map.put("110p", new BoxConfig("110p", 28, 55, 56, 87, 95, Arrays.asList("100p", "110p", "120p")));
            map.put("120p", new BoxConfig("120p", 30, 60, 61, 80, 86, Arrays.asList("110p", "120p", "130p")));
            map.put("130p", new BoxConfig("130p", 33, 65, 66, 74, 79, Arrays.asList("120p", "130p", "140p")));
            map.put("140p", new BoxConfig("140p", 35, 70, 71, 69, 73, Arrays.asList("130p", "140p", "150p")));
            map.put("150p", new BoxConfig("150p", 37, 75, 76, 65, 68, Arrays.asList("140p", "150p")));

        }

        return map;
    }
}
