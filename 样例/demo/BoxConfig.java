package kaoman.bean;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.ArrayList;
import java.util.List;

@Data
@AllArgsConstructor
@NoArgsConstructor
public class BoxConfig {


    private String spec;//规格名称

    private Integer preload;//预装条数

    private Integer minFishCount;//装箱最小数量

    private Integer maxFishCount;//装箱最大数量

    private Integer minFishWeight;//最小鱼重量

    private Integer maxFishWeight;//最大鱼重量

    private List<String> specList; //可匹配批次

    private List<Integer> fishWeightList; //可匹配批次

    public BoxConfig(String spec,Integer minFishCount, Integer maxFishCount, Integer minFishWeight, Integer maxFishWeight,List<String> specList) {
        this.spec = spec;
        this.minFishCount = minFishCount;
        this.maxFishCount = maxFishCount;
        this.minFishWeight = minFishWeight;
        this.maxFishWeight = maxFishWeight;
        this.specList = specList;
    }
    public BoxConfig(Integer minFishCount, Integer maxFishCount, Integer minFishWeight, Integer maxFishWeight) {
        this.minFishCount = minFishCount;
        this.maxFishCount = maxFishCount;
        this.minFishWeight = minFishWeight;
        this.maxFishWeight = maxFishWeight;
    }

    public BoxConfig(String spec, Integer preload, Integer minFishCount, Integer maxFishCount, Integer minFishWeight, Integer maxFishWeight, List<String> specList) {
        this.spec = spec;
        this.preload = preload;
        this.minFishCount = minFishCount;
        this.maxFishCount = maxFishCount;
        this.minFishWeight = minFishWeight;
        this.maxFishWeight = maxFishWeight;
        this.specList = specList;
    }

    //生产鱼数组
    public void setFishWeightList(int boxUnit) {
        int start = minFishWeight;
        int end = maxFishWeight;
        List<Integer> result = new ArrayList<>();
        while (start <= end){
            result.add(start);
            start += boxUnit;
        }
        this.fishWeightList = result;
    }
}
