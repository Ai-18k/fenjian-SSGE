package kaoman.bean;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@AllArgsConstructor
@NoArgsConstructor
public class Fish {

    int id; //编号

    int weight;//重量

    private int poolIndex; //所属缓冲池索引

    private String spec;//规格

    private int type;//类型


    public String getPrintStr(){
        return id + "-" + weight+ "-" + spec;
    }

    public Fish(int id, int weight, int poolIndex) {
        this.id = id;
        this.weight = weight;
        this.poolIndex = poolIndex;
    }
    public Fish(int id, int weight, int type, String spec) {
        this.id = id;
        this.weight = weight;
        this.type = type;
        this.spec = spec;
    }
}
