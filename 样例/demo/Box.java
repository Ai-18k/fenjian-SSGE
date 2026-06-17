package kaoman.bean;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Collectors;

@Data
@AllArgsConstructor
@NoArgsConstructor
public class Box {

    public static final int MAX_WEIGHT = 5100;  // 最大重量
    public static final int MIN_WEIGHT = 5000;  // 最小重量
    public static final int MIN_FISH_PER_BOX = 10;  // 每箱最小鱼条数
    public static final int MAX_FISH_PER_BOX = 11;  // 每箱最大鱼条数
    public static final int MIN_FISH_WEIGHT = 446;  // 每条鱼最小重量
    public static final int MAX_FISH_WEIGHT = 565;  // 每条鱼最大重量

    private String spec;// = new ArrayList<>();
    private List<Fish> fishList;// = new ArrayList<>();
    int currentWeight = 0;
    int fishCount = 0;

    public Box(List<Fish> fishList,int currentWeight) {
        this.currentWeight = currentWeight;
        this.fishList = fishList;
        this.fishCount = fishList != null ? fishList.size() : 0;
    }

    public static String getPrintStr(List<Fish> fishList) {
        StringBuilder str = new StringBuilder();
        List<Fish> collect = fishList.stream().sorted(Comparator.comparing(Fish::getId)).collect(Collectors.toList());
        for (Fish fish : collect) {
            if (str.length() > 0) {
                str.append(",");
            }
            str.append(fish.getPrintStr());
        }
        return str.toString();
    }

    public boolean addFish(Fish fish) {
        // 只有在箱子重量不超限且鱼条数没有达到最大限制时，才可以加鱼
        if (currentWeight + fish.weight <= MAX_WEIGHT && fishList.size() < MAX_FISH_PER_BOX) {
            //必须加完之后剩余下空间还能加进该规格鱼才能进入
            fishList.add(fish);
            currentWeight += fish.weight;
            fishCount = fishList.size();
            return true;
        }
        return false;
    }

    // 检查当前箱子是否符合要求
    public boolean isValid() {
        System.out.println("重量" + currentWeight + "xxx" + (currentWeight >= MIN_WEIGHT) + (currentWeight < MAX_WEIGHT));
        System.out.println("数量" + fishList.size() + "xxx" + (fishList.size() >= MIN_FISH_PER_BOX) + (fishList.size() <= MAX_FISH_PER_BOX));
        return fishList.size() >= MIN_FISH_PER_BOX && fishList.size() <= MAX_FISH_PER_BOX
                && currentWeight >= MIN_WEIGHT && currentWeight < MAX_WEIGHT;
    }

    // 获取箱子当前鱼的数量
    public int getFishCount() {
        return fishCount;
    }

    // 获取箱子的当前重量
    public int getWeight() {
        return currentWeight;
    }

    // 清空箱子
    public void clear() {
        fishList.clear();
        currentWeight = 0;
        fishCount = 0;
    }

    public List<Fish> getFishList() {
        return fishList;
    }


    public void setFishList(List<?> fishList) {
        if (fishList != null && !fishList.isEmpty() && fishList.get(0) instanceof Fish) {
            this.fishList = (List<Fish>) fishList;
            this.fishCount = fishList.size();
            this.currentWeight = fishList.stream().filter(f -> f instanceof Fish).mapToInt(f -> ((Fish) f).getWeight()).sum();
        } else if (fishList == null || fishList.isEmpty()) {
            this.fishList = new ArrayList<>();  // 处理空列表情况
            this.fishCount = 0;
            this.currentWeight = 0;
        } else {
            throw new IllegalArgumentException("List must contain Fish objects");
        }
    }

    public void setCurrentWeight(int currentWeight) {
        this.currentWeight = currentWeight;
    }


}
